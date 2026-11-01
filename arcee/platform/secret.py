# pyright: basic, reportMissingImports=false
"""The user's GNOME Keyring through libsecret, for the API keys set in the app."""

from arcee.keys import SCHEMA_NAME


class SecretKeyring:
    """The user's default GNOME Keyring collection."""

    def __init__(self) -> None:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret

        self._secret = Secret
        self._schema = Secret.Schema.new(
            SCHEMA_NAME, Secret.SchemaFlags.NONE, {"name": Secret.SchemaAttributeType.STRING}
        )

    def lookup(self, env: str) -> str | None:
        return self._secret.password_lookup_sync(self._schema, {"name": env}, None)

    def store(self, env: str, value: str) -> None:
        self._secret.password_store_sync(
            self._schema,
            {"name": env},
            self._secret.COLLECTION_DEFAULT,
            f"Arcee: {env}",
            value,
            None,
        )

    def clear(self, env: str) -> None:
        self._secret.password_clear_sync(self._schema, {"name": env}, None)
