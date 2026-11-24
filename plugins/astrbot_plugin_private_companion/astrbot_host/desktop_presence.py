"""Whether the paired desktop app is open, so unprompted output can skip it.

The desktop client opens one screen session when its window starts and ends it
when the window closes (``/desktop/screen`` actions); a chat message from the
paired device also counts as open.  The client has no periodic heartbeat.
Presence is kept in memory on the controller, so after a host restart the
desktop counts as absent until the client opens a session or sends a message.

``connect``/``disconnect`` are the whole port: the upcoming desktop session
(``desktop_session_enabled``) can feed them from its own register/disconnect.
"""

DESKTOP_PLATFORM = "webchat"


class DesktopPresence:
    def __init__(self):
        self._session = None

    def connect(self, session_id):
        self._session = session_id

    def disconnect(self, session_id=None):
        if session_id is None or session_id == self._session:
            self._session = None

    @property
    def present(self):
        return self._session is not None

    def allows(self, scope):
        """False only for the desktop WebChat window while the app is closed."""
        return scope.platform != DESKTOP_PLATFORM or self.present
