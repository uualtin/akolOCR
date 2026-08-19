# Runtime secrets

Create these files on the production VM with mode `0600` and never commit them:

- `database_password`
- `postgres_admin_password`
- `entry_camera_password`
- `exit_camera_password`
- `entry_gate_token`
- `exit_gate_token`

Gate token files may be empty while the corresponding driver is `disabled`.
The PostgreSQL admin password is used only for first-cluster bootstrap and the
explicit maintenance restore test; application containers use `database_password`.
