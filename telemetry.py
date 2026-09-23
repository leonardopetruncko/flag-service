"""Configuração do OpenTelemetry: traces, métricas e correlação de logs.

Os dados saem via OTLP (gRPC) para o OTel Collector do cluster, que roteia cada sinal
para o backend certo (Datadog, Prometheus e Loki). O endpoint, o nome do serviço e os
atributos vêm das variáveis OTEL_* padrão (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME,
OTEL_RESOURCE_ATTRIBUTES). Sem OTEL_EXPORTER_OTLP_ENDPOINT (dev local, testes e CI),
a telemetria fica desligada e o código roda igual.
"""

import logging
import os

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] %(message)s"


def setup_telemetry(flask_app=None, psycopg2=False, botocore=False):
    """Liga o SDK do OpenTelemetry e as instrumentações automáticas pedidas.

    Precisa rodar ANTES de criar o pool do PostgreSQL ou os clientes do boto3,
    porque as instrumentações substituem as funções de conexão dessas libs.
    """
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        log.info("OTEL_EXPORTER_OTLP_ENDPOINT não definida: telemetria desativada.")
        return

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # O Resource.create() lê OTEL_SERVICE_NAME e OTEL_RESOURCE_ATTRIBUTES.
    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15000)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    # Chamadas HTTP de saída (ex.: /validate no auth-service) levam o traceparent.
    # O analytics-service não usa a lib requests, por isso a instrumentação é opcional.
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument()
    except ImportError:
        log.info("Lib 'requests' ausente: instrumentação HTTP de saída desativada.")

    if flask_app is not None:
        from opentelemetry.instrumentation.flask import FlaskInstrumentor

        FlaskInstrumentor().instrument_app(flask_app, excluded_urls="health")

    if psycopg2:
        from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

        # skip_dep_check: a imagem usa o pacote psycopg2-binary, não o psycopg2.
        Psycopg2Instrumentor().instrument(skip_dep_check=True)

    if botocore:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

        BotocoreInstrumentor().instrument()

    # Coloca trace_id/span_id em cada linha de log: no Grafana dá para ir do log (Loki)
    # direto ao trace (Datadog) pelo mesmo ID.
    LoggingInstrumentor().instrument()
    # Os defaults cobrem logs criados antes da instrumentação (sem trace ativo).
    formatter = logging.Formatter(LOG_FORMAT, defaults={"otelTraceID": "0", "otelSpanID": "0"})
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)

    log.info("OpenTelemetry inicializado (traces + métricas via OTLP).")
