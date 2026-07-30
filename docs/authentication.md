# Authentication

mcp-zuul supports three authentication modes: anonymous, token-based, and Kerberos/SPNEGO.

## Anonymous

No configuration needed. Works with public Zuul instances:

```bash
claude mcp add zuul -e ZUUL_URL=https://softwarefactory-project.io/zuul -- uvx mcp-zuul
```

## Token authentication

Set `ZUUL_AUTH_TOKEN` via host environment — **never hardcode tokens in config files**:

```bash
export ZUUL_AUTH_TOKEN=<your-token>
```

For Docker, forward without a value to inherit from host:

```json
"args": ["run", "-i", "--rm", "-e", "ZUUL_AUTH_TOKEN", "mcp-zuul"]
```

## Kerberos / SPNEGO

For Zuul behind OIDC + Kerberos. Requires a valid Kerberos ticket (`kinit`) and the `gssapi` package.

### Prerequisites

=== "Fedora/RHEL/CentOS"

    ```bash
    sudo dnf install krb5-devel python3-devel gcc
    ```

=== "Debian/Ubuntu"

    ```bash
    sudo apt install libkrb5-dev python3-dev gcc
    ```

=== "macOS / Windows"

    Pre-built wheels available — no extra packages needed.

### Install with Kerberos support

```bash
pip install mcp-zuul[kerberos]
# or
uvx --with "mcp-zuul[kerberos]" mcp-zuul
```

### Configure

=== "CLI"

    ```bash
    claude mcp add -s user zuul-internal \
                   -e ZUUL_URL=https://internal-zuul.example.com/zuul \
                   -e ZUUL_DEFAULT_TENANT=my-tenant \
                   -e ZUUL_USE_KERBEROS=true \
                   -e ZUUL_VERIFY_SSL=false \
                   -- uvx --with "mcp-zuul[kerberos]" mcp-zuul
    ```

=== "JSON"

    ```json
    {
      "zuul-internal": {
        "command": "uvx",
        "args": ["--with", "mcp-zuul[kerberos]", "mcp-zuul"],
        "env": {
          "ZUUL_URL": "https://internal-zuul.example.com/zuul",
          "ZUUL_USE_KERBEROS": "true",
          "ZUUL_VERIFY_SSL": "false"
        }
      }
    }
    ```

=== "Docker"

    ```bash
    docker run -i --rm \
      -v /etc/krb5.conf:/etc/krb5.conf:ro \
      -v /tmp/krb5cc_$(id -u):/tmp/krb5cc_$(id -u):ro \
      -e KRB5CCNAME=/tmp/krb5cc_$(id -u) \
      -e ZUUL_URL=https://internal-zuul.example.com/zuul \
      -e ZUUL_USE_KERBEROS=true \
      mcp-zuul
    ```

### How it works

mcp-zuul drives the full SPNEGO redirect chain automatically:

1. Initial request to Zuul API
2. Redirect to OIDC identity provider
3. SPNEGO negotiation using the Kerberos ticket
4. Session cookie established
5. Subsequent requests use the session cookie
6. Re-authentication on cookie expiry (transparent, serialized via lock)

The `_BearerAuth` httpx subclass ensures tokens are stripped on cross-origin redirects to prevent credential leakage.
