export interface GovernanceRulesSnapshot {
  rules: string[]
  activeRules: string[]
}

export type GovernanceCommandSender = (
  command: string,
  payload?: unknown
) => void
