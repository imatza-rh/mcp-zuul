#!/usr/bin/env bash
# release.sh - Automated release pipeline for mcp-zuul
#
# Usage:
#   ./release.sh <version>          # e.g. ./release.sh 0.5.0
#   ./release.sh patch|minor|major  # auto-bump from current version
#
# Steps: validate → bump → commit → push → tag → PyPI → GH release → MCP registry
# Aborts on any failure. Requires: uv, gh, git, security (macOS keychain).

set -euo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────────

die()  { printf '\033[31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$1"; }
ok()   { printf '\033[32m ✓\033[0m %s\n' "$1"; }

# ── Parse version ────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

[[ $# -eq 1 ]] || die "Usage: $0 <version|patch|minor|major>"

CURRENT=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
[[ -n "$CURRENT" ]] || die "Could not read current version from pyproject.toml"

bump_version() {
    local cur="$1" part="$2"
    IFS='.' read -r major minor patch <<< "$cur"
    case "$part" in
        major) echo "$((major + 1)).0.0" ;;
        minor) echo "${major}.$((minor + 1)).0" ;;
        patch) echo "${major}.${minor}.$((patch + 1))" ;;
        *)     die "Unknown bump type: $part" ;;
    esac
}

case "$1" in
    patch|minor|major) VERSION=$(bump_version "$CURRENT" "$1") ;;
    [0-9]*)            VERSION="$1" ;;
    *)                 die "Invalid argument: $1 (expected version or patch|minor|major)" ;;
esac

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Invalid version format: $VERSION"
[[ "$VERSION" != "$CURRENT" ]] || die "Version $VERSION is already current"

info "Releasing mcp-zuul v${VERSION} (current: ${CURRENT})"

# ── Pre-flight checks ───────────────────────────────────────────────────────

info "Pre-flight checks"

[[ "$(git branch --show-current)" == "main" ]] || die "Not on main branch"
[[ -z "$(git status --porcelain)" ]] || die "Working tree is dirty"

git fetch origin main --quiet
BEHIND=$(git rev-list --count HEAD..origin/main)
[[ "$BEHIND" -eq 0 ]] || die "Local main is ${BEHIND} commits behind origin/main (rebase first)"

command -v uv  >/dev/null || die "uv not found"
command -v gh  >/dev/null || die "gh not found"

# Check PyPI token is accessible
security find-generic-password -a pypi -s mcp-zuul -w >/dev/null 2>&1 \
    || die "PyPI token not found in keychain (service: mcp-zuul, account: pypi)"

git tag -l "v${VERSION}" | grep -q . && die "Tag v${VERSION} already exists"

grep -q "^## \\[${VERSION}\\]" CHANGELOG.md \
    || die "No CHANGELOG.md entry for [${VERSION}] - add it before releasing"

ok "All pre-flight checks passed"

# ── Validate ─────────────────────────────────────────────────────────────────

info "Running validation suite"

uv run ruff check src/ tests/
ok "Lint passed"

uv run ruff format --check src/ tests/
ok "Format check passed"

uv run mypy src/mcp_zuul/
ok "Type check passed"

uv run pytest tests/ -v --tb=short
ok "Tests passed"

# ── Bump version ─────────────────────────────────────────────────────────────

info "Bumping version: ${CURRENT} → ${VERSION}"

# pyproject.toml: version = "X.Y.Z"
sed -i '' "s/^version = \"${CURRENT}\"/version = \"${VERSION}\"/" pyproject.toml

# server.json: two occurrences of the version string
sed -i '' "s/\"${CURRENT}\"/\"${VERSION}\"/g" server.json

# Verify the bump took effect
grep -q "\"${VERSION}\"" pyproject.toml || die "pyproject.toml version bump failed"
[[ $(grep -c "\"${VERSION}\"" server.json) -eq 2 ]] || die "server.json version bump failed (expected 2 occurrences)"

ok "Version bumped in pyproject.toml and server.json"

# ── Commit and push ─────────────────────────────────────────────────────────

info "Committing version bump"

git add pyproject.toml server.json
git commit -s -m "[CHORE] Bump version to ${VERSION}"
ok "Committed"

info "Pushing to origin/main"
git push origin main
ok "Pushed"

# ── Tag ──────────────────────────────────────────────────────────────────────

info "Creating tag v${VERSION}"

git tag -a "v${VERSION}" -m "v${VERSION}"
git push origin "v${VERSION}"
ok "Tag v${VERSION} pushed (Docker build triggered)"

# ── PyPI ─────────────────────────────────────────────────────────────────────

info "Publishing to PyPI"

rm -rf dist/
uv build

( set +x; UV_PUBLISH_TOKEN=$(security find-generic-password -a pypi -s mcp-zuul -w) uv publish )
ok "Published to PyPI"

# ── GitHub Release ───────────────────────────────────────────────────────────

info "Creating GitHub Release"

# Extract changelog section for this version
NOTES=$(awk "/^## \\[${VERSION}\\]/{found=1; next} /^## \\[/{if(found) exit} found" CHANGELOG.md)
[[ -n "$NOTES" ]] || NOTES="Release v${VERSION}"

gh release create "v${VERSION}" --title "v${VERSION}" --notes "$NOTES"
ok "GitHub Release created"

# ── MCP Registry ─────────────────────────────────────────────────────────────

info "Triggering MCP Registry publish"
gh workflow run publish-registry.yml
ok "MCP Registry workflow dispatched"

# ── Done ─────────────────────────────────────────────────────────────────────

printf '\n\033[32m════════════════════════════════════════\033[0m\n'
printf '\033[32m  mcp-zuul v%s released!\033[0m\n' "$VERSION"
printf '\033[32m════════════════════════════════════════\033[0m\n\n'

echo "  PyPI:     https://pypi.org/project/mcp-zuul/${VERSION}/"
echo "  GitHub:   https://github.com/imatza-rh/mcp-zuul/releases/tag/v${VERSION}"
echo "  Docker:   https://github.com/imatza-rh/mcp-zuul/actions/workflows/docker.yml"
echo "  Registry: https://github.com/imatza-rh/mcp-zuul/actions/workflows/publish-registry.yml"
echo ""
echo "Remember to update CLAUDE.md if tool count changed."
