FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the agent source
COPY . .

# Run as non-root (matching the pod securityContext UID/GID)
RUN groupadd -g 10001 agent && \
    useradd -u 10001 -g 10001 -s /bin/false agent
USER 10001:10001

# The gateway binary will be mounted at /shared/gateway by the initContainer
ENTRYPOINT ["python", "-m", "agent"]
