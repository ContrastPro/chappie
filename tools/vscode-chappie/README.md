# Chappie VS Code extension

This is a private/internal Chappie companion extension. It is distributed to
workstations through the versioned VSIX in `dist/`; it is not published to the
VS Code Marketplace.

Node/npm are required only to rebuild the extension artifact. Normal Chappie
setup verifies the repository-tracked SHA-256 and installs the local VSIX via
the VS Code CLI without invoking Node/npm or downloading an extension.

To release a new extension version, update `package.json`, run the package
workflow, replace `dist/chappie-vscode-<version>.vsix`, and regenerate
`dist/SHA256SUMS` together.
