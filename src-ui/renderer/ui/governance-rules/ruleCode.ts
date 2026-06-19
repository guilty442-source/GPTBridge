const PHRASE_CODE_MAP: Array<[string, string]> = [
  ['應用程式', 'application'],
  ['資料庫化', 'database'],
  ['資料庫', 'database'],
  ['模組化', 'module'],
  ['治理規則', 'governance_rule'],
  ['治理', 'governance'],
  ['規則', 'rule'],
  ['介面', 'ui'],
  ['中文化', 'zh_tw'],
  ['中文', 'zh'],
  ['全域', 'global'],
  ['啟動', 'enabled'],
  ['生效', 'effective'],
  ['修改', 'modify'],
  ['新增', 'add'],
  ['刪除', 'delete'],
  ['自動', 'auto'],
  ['轉換', 'convert'],
  ['程式碼', 'code'],
  ['啟動流程', 'startup_flow'],
  ['啟動', 'startup'],
  ['流程', 'flow'],
  ['安全', 'security'],
  ['命名', 'naming'],
  ['路徑', 'path'],
  ['匯入', 'import'],
  ['架構', 'architecture'],
  ['執行層', 'runtime_layer'],
  ['執行', 'runtime'],
  ['隔離', 'isolation'],
]

const CJK_PATTERN = /[\u3400-\u9fff]/
const CODE_PATTERN = /^[A-Za-z][A-Za-z0-9_-]*$/

function collapseSeparators(value: string): string {
  return value
    .replace(/[^a-zA-Z0-9_-]+/g, '_')
    .replace(/-+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '')
    .toLowerCase()
}

function encodeUnknownChar(char: string): string {
  return `u${char.codePointAt(0)?.toString(16) ?? '0'}`
}

function replaceKnownPhrases(input: string): string {
  return [...PHRASE_CODE_MAP].sort((a, b) => b[0].length - a[0].length).reduce(
    (next, [phrase, code]) => next.split(phrase).join(` ${code} `),
    input
  )
}

export function hasChineseInput(input: string): boolean {
  return CJK_PATTERN.test(input)
}

export function toGovernanceRuleCode(input: string): string {
  const trimmed = input.trim()
  if (!trimmed) return ''
  if (CODE_PATTERN.test(trimmed)) return trimmed

  const phraseNormalized = replaceKnownPhrases(trimmed.normalize('NFKD'))
  let encoded = ''

  for (const char of phraseNormalized) {
    if (/[a-zA-Z0-9_-]/.test(char)) {
      encoded += char
      continue
    }
    if (CJK_PATTERN.test(char)) {
      encoded += `_${encodeUnknownChar(char)}_`
      continue
    }
    encoded += '_'
  }

  const slug = collapseSeparators(encoded)
  if (!slug) return ''
  if (slug.startsWith('rule_') || slug.startsWith('g_')) return slug
  return `rule_custom_${slug}`
}
