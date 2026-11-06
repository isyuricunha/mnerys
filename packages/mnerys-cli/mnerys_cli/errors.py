"""Exit codes and user-facing errors for the mnerys CLI.

Exit code contract:
0 success, 1 runtime/network/server error, 2 usage error,
3 configuration error, 4 authentication/authorization error (401/403),
5 cancelled by the user.
"""


class MnerysCliError(RuntimeError):
    exit_code = 1


class UsageError(MnerysCliError):
    exit_code = 2


class ConfigError(MnerysCliError):
    exit_code = 3


class AuthError(MnerysCliError):
    exit_code = 4


class Cancelled(MnerysCliError):
    exit_code = 5