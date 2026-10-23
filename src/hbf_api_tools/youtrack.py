"""Typed YouTrack operations; credentials and endpoint are constrained."""
from __future__ import annotations

import re
import urllib.parse

from .config import ConfigurationError, load_scalar_yaml
from .http import request_json
from .identity import acting_agent, add_attribution

ISSUE_ID = re.compile(r"^[A-Z][A-Z0-9]+-[1-9][0-9]{0,7}$")


class YouTrack:
    def __init__(self):
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
        actor = acting_agent()
        user = request_json(self.base, "/api/users/me", self.token,
                            query={"fields": "login"})
        if user.get("login") != actor:
            raise ConfigurationError("Refusing YouTrack mutation: token does not match local agent identity")
        return actor

    def add_comment(self, issue_id: str, text: str):
        text = text.strip()
        if not 1 <= len(text) <= 10_000:
            raise ValueError("Comment must contain 1..10000 characters")
        actor = self._confirm_agent()
        text = add_attribution(text, actor)
        if len(text) > 10_000:
            raise ValueError("Comment with agent attribution exceeds the limit")
        result = request_json(self.base, self._issue(issue_id) + "/comments", self.token,
                              method="POST", payload={"text": text},
                              query={"fields": "id,author(login)"})
        author = result.get("author") or {}
        if author.get("login") != actor:
            raise RuntimeError("YouTrack did not confirm acting agent as the comment author")
        return {"id": result.get("id"), "author": actor}
