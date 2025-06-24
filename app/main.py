import json
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from redis import Redis
from kafka import KafkaConsumer, KafkaProducer
from prometheus_api_client import PrometheusConnect
from pybreaker import CircuitBreaker
from evidently.report import Report
from evidently.metrics import DatasetDriftMetric
import threading
import logging
from datetime import datetime, timedelta
import joblib
from tensorflow.keras.models import load_model
from sklearn.preprocessing import StandardScaler

# --- Configuration ---
app = FastAPI()
logging.basicConfig(level=logging.INFO)

# Security
API_KEY = "your-secret-key"
api_key_header = APIKeyHeader(name="X-API-Key")

# Services
redis = Redis(host="redis", port=6379, decode_responses=True)
producer = KafkaProducer(bootstrap_servers='kafka:9092',
                        value_serializer=lambda v: json.dumps(v).encode('utf-8'))

# Circuit Breaker
breaker = CircuitBreaker(fail_max=5, reset_timeout=60)

# Models
scaler = joblib.load('models/scaler.pkl')
iso_forest = joblib.load('models/iso_forest.pkl')
lstm_model = load_model('models/lstm.h5')

# --- Data Models ---
class Metrics(BaseModel):
    cpu: float
    memory: float
    error_rate: float
    latency: float
    db_connections: float
    timestamp: datetime = datetime.utcnow()

# --- Core Functions ---
@breaker
def predict_failure_risk(sequence: np.ndarray) -> float:
    """Generate ensemble failure risk score"""
    # 1. Isolation Forest
    iso_score = iso_forest.decision_function(sequence[-1].reshape(1, -1))
    iso_score_normalized = 1 / (1 + np.exp(-iso_score))[0]
    
    # 2. LSTM Prediction
    lstm_score = lstm_model.predict(sequence[np.newaxis, ...], verbose=0)[0][0]
    
    # 3. Weighted Ensemble
    return 0.4 * iso_score_normalized + 0.6 * lstm_score

def check_drift(current_data: pd.DataFrame):
    """Monitor data drift using Evidently"""
    reference_data = pd.read_parquet('data/reference.parquet')
    
    report = Report(metrics=[
        DatasetDriftMetric(),
        ColumnDriftMetric(column_name='cpu'),
        ColumnDriftMetric(column_name='error_rate')
    ])
    
    report.run(
        current_data=current_data,
        reference_data=reference_data,
        column_mapping=ColumnMapping(
            numerical_features=['cpu', 'memory', 'error_rate', 'latency', 'db_connections']
        )
    )
    
    if report.as_dict()['metrics'][0]['result']['dataset_drift']:
        producer.send('alerts', value={'type': 'data_drift', 'severity': 'high'})

# --- API Endpoints ---
@app.post("/predict")
async def predict(metrics: Metrics, api_key: str = Security(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    
    try:
        # 1. Store in Redis buffer
        redis.lpush("metrics_buffer", metrics.json())
        redis.ltrim("metrics_buffer", 0, 9)  # Keep last 10 readings
        
        # 2. Prepare sequence
        buffer = [json.loads(item) for item in redis.lrange("metrics_buffer", 0, 9)]
        if len(buffer) < 10:
            return {"status": "buffering", "samples": len(buffer)}
        
        # 3. Scale and predict
        df = pd.DataFrame(buffer).drop(columns=['timestamp'])
        sequence = scaler.transform(df)
        risk_score = predict_failure_risk(sequence)
        
        # 4. Check for drift
        check_drift(df.tail(100))  # Monitor last 100 samples
        
        # 5. Publish to Kafka
        producer.send('predictions', value={
            'timestamp': datetime.utcnow().isoformat(),
            'risk_score': risk_score,
            'metrics': metrics.dict()
        })
        
        return {"risk_score": risk_score}
    
    except Exception as e:
        logging.error(f"Prediction failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Prediction service unavailable")

# --- Background Workers ---
def prometheus_fetcher():
    """Periodically fetch metrics from Prometheus"""
    prom = PrometheusConnect(url="http://prometheus:9090")
    while True:
        try:
            df = fetch_real_metrics(prom)
            producer.send('raw_metrics', value=df.to_dict(orient='records'))
            threading.Event().wait(60)  # Run every minute
        except Exception as e:
            logging.error(f"Prometheus fetch failed: {e}")

def kafka_consumer():
    """Process incoming metrics from Kafka"""
    consumer = KafkaConsumer(
        'raw_metrics',
        bootstrap_servers='kafka:9092',
        value_deserializer=lambda v: json.loads(v.decode('utf-8'))
    )
    for message in consumer:
        try:
            data = message.value
            # Process and store in Redis
            redis.lpush("metrics_buffer", json.dumps(data))
        except Exception as e:
            logging.error(f"Kafka processing error: {e}")

# --- Startup Event ---
@app.on_event("startup")
async def startup_event():
    # Start background workers
    threading.Thread(target=prometheus_fetcher, daemon=True).start()
    threading.Thread(target=kafka_consumer, daemon=True).start()
