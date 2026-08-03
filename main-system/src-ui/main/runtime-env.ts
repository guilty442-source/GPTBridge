export function getRuntimeEnv(name: string): string | undefined {
  return process['env'][name]
}

export function getRuntimeEnvMap(): NodeJS.ProcessEnv {
  return process['env']
}
