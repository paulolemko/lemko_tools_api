



# przebuduj i podnieś TYLKO backend
docker compose build asr
docker compose logs -f asr
docker compose up -d --no-deps --force-recreate asr

docker compose build asr && docker compose up -d asr
Rebuild and restart the backend (docker compose build asr && docker compose up -d --no-deps --force-recreate asr) to pick up the new dependencies and copied binaries.


# logi
docker compose logs --tail=100 asr

# lokalnie (host)
curl -s http://127.0.0.1:8000/healthz

# przez Apache
curl -I https://apiasr.spektrogram.com/healthz


docker compose up -d --no-deps --force-recreate asr
curl -s https://apiasr.spektrogram.com/healthz

edycja slownika
https://slowniklt.spektrogram.com/users/sign_in

czyszczenie:
docker system prune -a --volumes

działanie przez dockera:
docker exec -it lemko-asr   python3 /app/models/term-transfer.py import   --input /app/models/term_8581.json
docker exec -it lemko-asr   python3 /app/models/fill_missing_adjective_forms.py --term-id 12886