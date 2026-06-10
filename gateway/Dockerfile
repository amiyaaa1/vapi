FROM golang:1.23-alpine AS build
WORKDIR /app
COPY go.* ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 go build -o gateway .

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/gateway ./gateway
COPY scripts ./scripts
RUN pip install --no-cache-dir curl_cffi
RUN mkdir -p /data
CMD ["./gateway"]
