"""Typed YouTrack operations; credentials and endpoint are constrained."""
from __future__ import annotations

import re
import urllib.parse

from .config import ConfigurationError, load_scalar_yaml
from .http import request_json
from .identity import acting_agent, add_attribution

ISSUE_ID = re.compile(r"^[A-Z][A-Z0-9]+-[1-9][0-9]{0,7}$")


class YouTrack:
    def __init__(self, *, identity_provider=None, attribution_provider=None):
        # Internal dependency injection for MCP service identities; CLI defaults stay host-bound.
        self._identity_provider = identity_provider or acting_agent
        self._attribution_provider = attribution_provider or add_attribution
        cfg = load_scalar_yaml("youtrack.yml")
        self.base = cfg.get("youtrack_url", "").rstrip("/")
        self.token = cfg.get("youtrack_token_value", "")
        parsed = urllib.parse.urlsplit(self.base)
        if (parsed.scheme != "https" or parsed.hostname != "youtrack.hbf.pl"
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ConfigurationError("YouTrack credential must use trusted HTTPS origin")
        if not self.token or any(ord(char) < 33 or ord(char) > 126 for char in self.token):
            raise ConfigurationError("YouTrack token is missing or invalid")

    def _issue(self, issue_id: str) -> str:
        if not isinstance(issue_id, str) or not ISSUE_ID.fullmatch(issue_id):
            raise ValueError("Expected a valid YouTrack issue ID")
        return "/api/issues/" + urllib.parse.quote(issue_id, safe="")

    def search(self, query: str = "", limit: int = 15, skip: int = 0):
        if not 1 <= limit <= 100 or not 0 <= skip <= 100_000:
            raise ValueError("Invalid pagination bounds")
        self._confirm_agent()
        return request_json(self.base, "/api/issues", self.token, query={
            "query": query, "$top": limit, "$skip": skip,
            "fields": "id,idReadable,summary,project(name),customFields(name,value(name))",
        })

    def issue(self, issue_id: str):
        route = self._issue(issue_id)
        self._confirm_agent()
        return request_json(self.base, route, self.token, query={
            "fields": "id,idReadable,summary,description,created,updated,project(name),customFields(name,value(name))",
        })

    def comments(self, issue_id: str, limit: int = 20):
        if not 1 <= limit <= 100:
            raise ValueError("Limit must be 1..100")
        route = self._issue(issue_id) + "/comments"
        self._confirm_agent()
        return request_json(self.base, route, self.token,
                            query={"$top": limit, "fields": "id,text,created,author(login,name)"})

    def _confirm_agent(self) -> str:
        actor = self._identity_provider()
        user = request_json(self.base, "/api/users/me", self.token,
                            query={"fields": "login"})
        if user.get("login") != actor:
            raise ConfigurationError("Refusing YouTrack mutation: token does not match local agent identity")
        return actor

    def set_state(self, issue_id: str, state: str) -> dict:
        if not isinstance(state, str) or not 1 <= len(state.strip()) <= 100:
            raise ValueError("State must contain 1..100 characters")
        state = state.strip()
        actor = self._confirm_agent()
        route = self._issue(issue_id)
        fields = "id,idReadable,customFields(name,$type,value(name,$type))"
        current = request_json(self.base, route, self.token, query={"fields": fields})
        matches = [field for field in current.get("customFields", [])
                   if field.get("name") == "State"]
        if len(matches) != 1 or matches[0].get("$type") != "StateIssueCustomField":
            raise ValueError("Issue does not expose one directly writable State field")
        current_state = (matches[0].get("value") or {}).get("name")
        if current_state == state:
            return {"id": current.get("idReadable", issue_id),
                    "state": state, "changed": False, "author": actor}
        result = request_json(self.base, route, self.token, method="POST",
                              payload={"customFields": [{
                                  "name": "State",
                                  "$type": "StateIssueCustomField",
                                  "value": {"name": state, "$type": "StateBundleElement"},
                              }]},
                              query={"fields": fields})
        updated = [field for field in result.get("customFields", [])
                   if field.get("name") == "State"]
        if len(updated) != 1 or (updated[0].get("value") or {}).get("name") != state:
            raise RuntimeError("YouTrack did not confirm the requested state")
        return {"id": result.get("idReadable", current.get("idReadable", issue_id)),
                "state": state, "changed": True, "author": actor}

    def add_comment(self, issue_id: str, text: str):
        text = text.strip()
        if not 1 <= len(text) <= 10_000:
            raise ValueError("Comment must contain 1..10000 characters")
        actor = self._confirm_agent()
        text = self._attribution_provider(text, actor)
        if len(text) > 10_000:
            raise ValueError("Comment with agent attribution exceeds the limit")
        result = request_json(self.base, self._issue(issue_id) + "/comments", self.token,
                              method="POST", payload={"text": text},
                              query={"fields": "id,author(login)"})
        author = result.get("author") or {}
        if author.get("login") != actor:
            raise RuntimeError("YouTrack did not confirm acting agent as the comment author")
        return {"id": result.get("id"), "author": actor}
