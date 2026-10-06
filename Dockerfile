# Stage 1: Build the static Go binary
FROM golang:1.24-alpine AS builder

# Install ca-certificates and git
RUN apk update && apk add --no-cache git ca-certificates tzdata && update-ca-certificates

WORKDIR /app

# Copy go mod and sum files
COPY go.mod go.sum ./
RUN go mod download

# Copy the rest of the source code
COPY . .

# Build a statically linked binary
# CGO_ENABLED=0 ensures it doesn't dynamically link against libc
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -a -installsuffix cgo -ldflags="-w -s" -o gateway ./cmd/gateway

# Stage 2: Create the minimal scratch container
FROM scratch

# Copy TLS certificates from the builder stage
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# Copy the static binary
COPY --from=builder /app/gateway /gateway

# Run the binary
ENTRYPOINT ["/gateway"]
