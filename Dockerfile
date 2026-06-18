FROM golang:1.23-alpine AS build

WORKDIR /src

COPY gateway/go.mod ./
COPY gateway/go.sum* ./
RUN go mod download

COPY gateway/*.go ./
RUN CGO_ENABLED=0 go build -buildvcs=false -o /out/gateway .

FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

ENV PYTHONUNBUFFERED=1 \
    VAPI_CHAT_HELPER=/app/scripts/vapi_chat.py

WORKDIR /app

COPY --from=build /out/gateway /app/gateway
COPY requirements.txt /app/requirements.txt
COPY nexos_solver/requirements.txt /app/nexos_solver/requirements.txt
COPY vendor/CloakBrowser /tmp/CloakBrowser

RUN pip install --no-cache-dir -r /app/requirements.txt \
  && pip install --no-cache-dir -r /app/nexos_solver/requirements.txt \
  && pip install --no-cache-dir /tmp/CloakBrowser \
  && python3 -m patchright install chromium \
  && python3 -c "from cloakbrowser import ensure_binary; print('CloakBrowser binary:', ensure_binary())" \
  && mkdir -p /data

COPY gateway/scripts /app/scripts
COPY registrator /app/registrator
COPY nexos_solver /app/nexos_solver
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 3100

CMD ["/app/start.sh"]
