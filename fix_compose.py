#!/usr/bin/env python3
with open('docker-compose.yml', 'r') as f:
    content = f.read()

# Fix the malformed DATABASE_URL
content = content.replace(
    '${POSTGRES_USER:[REDACTED]@postgres:5432/',
    '${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/'
)

with open('docker-compose.yml', 'w') as f:
    f.write(content)

print("Fixed docker-compose.yml")
