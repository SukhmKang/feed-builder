### Nitter Session Rotation SOP

- Generate new `sessions.jsonl` locally

- Upload to server:
  ```bash
  scp /path/to/sessions.jsonl root@161.35.138.12:~/nitter/sessions.new.jsonl
  ```

- SSH into server:
  ```bash
  ssh root@161.35.138.12
  cd ~/nitter
  ```

- (Optional) Backup current sessions:
  ```bash
  cp sessions.jsonl sessions.backup.jsonl
  ```

- Replace sessions file:
  ```bash
  mv sessions.new.jsonl sessions.jsonl
  ```

- Restart Nitter:
  ```bash
  docker restart nitter
  ```

- Verify:
  ```bash
  docker logs --tail=50 nitter
  curl http://localhost:8080
  ```

- If broken, rollback:
  ```bash
  mv sessions.backup.jsonl sessions.jsonl
  docker restart nitter
  ```