# [Level 1] Startup Governance Rules

## 1. UI First Principle (G-001)
The renderer MUST display the MainShell within 500ms. No "Loading Gate" is allowed.

## 2. Pipeline Observability (G-103)
All startup tasks must register via `RuntimeServiceManager` and be observable.

## 3. Status Lifecycle
- INIT: Task started.
- SUCCESS: Task resolved within timeout.
- FAIL: Task rejected.
- TIMEOUT: Task exceeded its assigned time limit.
- DEGRADED: Non-critical task failed, but app continues.

## 4. Runtime Single Flow (G-100)
Only one official startup pipeline is allowed:
Renderer → MainShell → ServiceManager → Background Services → Health Monitor.

## 5. Service Dependency Governance (G-037)
All services must explicitly declare dependencies, timeouts, retry policies, and degraded behavior. Direct "hidden" dependencies are prohibited.