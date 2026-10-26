# Emby: multiple read-only instances

The HBF MCP bridge discovers configured Emby servers from regular files matching
`~/.credentials/emby*.yml` in the MCP service account's home directory.
The service never exports API tokens or accepts credentials or URLs from clients.

## Configure

Examples (each file should have mode 0600):

- `~/.credentials/emby-ds1522.yml` -> instance `ds1522`
- `~/.credentials/emby-bedroom.yml` -> instance `bedroom`
- `~/.credentials/emby.yml` -> instance `default`
- `~/.credentials/embyother.yml` -> instance `other`

Each file follows the same three-field format:

```yaml
emby_internal: http://192.168.1.50:8096/
emby_external: https://example-emby.example.org/
emby_token: YOUR_LOCAL_API_TOKEN
```

Credentials must remain outside Git. Symbolic links and invalid aliases are ignored.
Alias collisions are rejected rather than silently choosing one server.

## MCP tools

- `emby_instances()` lists aliases only, without touching secrets or APIs.
- `emby_status(instance="ds1522", target="internal")` verifies connectivity.
- `emby_library_counts(instance="ds1522")` gets library item counts.
- `emby_movies(instance="ds1522", limit=50, search_term="", year=0, missing_imdb_only=False)` pages movies (max 100 per call).
- `emby_movie_details(item_id="123", instance="ds1522")` reads one movie's metadata.

If exactly one instance exists, `instance` can be omitted. If more than one
instance exists, `instance` is required for every API call.

`target` defaults to `internal`; `external` uses HTTPS with the hostname from
the **selected local credential file**. Internal URLs must use private IPs.
API calls use fixed GET endpoints, do not follow redirects and bound responses.
Tokens are never exposed in results or exception messages.

## Test and deploy

```bash
.venv/bin/python -m pytest -q
./install artur gptagent
```

Installation requires an operator with appropriate host privileges.
After deploying, refresh the MCP connector to discover the added tools.
Do not put credentials in source control or test fixtures.
