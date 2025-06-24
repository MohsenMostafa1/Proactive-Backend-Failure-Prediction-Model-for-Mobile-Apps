Key Features Implemented:

    Real Data Pipeline:

        Prometheus metrics fetcher

        Kafka streaming integration

        Redis sequence buffering

    Production-Ready API:

        API key authentication

        Circuit breaker pattern

        Proper error handling

    Monitoring:

        Evidently drift detection

        Kafka alerts channel

        Prometheus metrics

    Scalable Architecture:

        Microservice design

        Background workers

        Containerized deployment

To run the complete system:

bash

docker-compose up -d


The system will:

    Fetch real metrics from Prometheus every minute

    Process them through Kafka

    Maintain a 10-sample buffer in Redis

    Provide predictions via HTTP endpoint

    Monitor for data drift

    Scale horizontally by adding more app instances
