FROM python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8
COPY integrations/redfish/server.py /app/server.py
USER 1000:1000
ENTRYPOINT ["python3", "-u", "/app/server.py"]
