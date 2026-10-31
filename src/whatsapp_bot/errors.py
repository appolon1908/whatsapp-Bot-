class CommerceError(Exception):
    """Safe public domain error, never an underlying DB/provider exception."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
