\# Deployment (Docker Compose Mini-Prod)



Start:

docker compose up -d --build



Stop:

docker compose down



Check status:

docker compose ps



Health:

curl http://127.0.0.1:8000/health

