# [Level 4] Technical Debt Registry (G-045)

| Debt ID | Origin | Impact | Temporary Reason | Cleanup Target | Priority |
| :--- | :--- | :--- | :--- | :--- | :--- |
| DEBT-001 | Plugins Initialization | Sandbox only, no real isolation. | Current IPC limitation. | G-014 implementation | Medium |
| DEBT-002 | Dashboard AST Guard | Manual audit only. | Complex AST rule setup. | G-015 automated checker| Low |

### Sunset Tracking
- Legacy Startup Flow: Deprecated in v3.0.0.
- Mirror Folder Logic: Targeted for removal in v4.0.0.