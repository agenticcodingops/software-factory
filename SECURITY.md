# Security policy

Report vulnerabilities through [GitHub private vulnerability reporting](https://github.com/agenticcodingops/software-factory/security/advisories/new).
If reporting is unavailable, ask a maintainer to enable it without publishing exploit details or credentials in an issue.

This project is under construction. The deployment controls in [ADR 0001](docs/adr/0001-azure-deployment.md) remain open until their implementation and acceptance tests pass. No production deployment is supported yet.

Keep credentials, subscription and tenant identifiers, personal e-mail addresses and real tailnet names in a private instance repository. Public examples use the placeholders described in [deny-list/README.md](deny-list/README.md). The deny-list supplements secret scanning; it does not prove that a tree is free of secrets.
