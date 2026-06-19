interface AutoIntervalFieldProps {
  value: number
  onChange: (minutes: number) => void
  disabled?: boolean
  label?: string
}

export function AutoIntervalField({
  value,
  onChange,
  disabled = false,
  label = '自動週期（分鐘）',
}: AutoIntervalFieldProps) {
  return (
    <div className="devm-keyval">
      <div className="devm-keyval-row">
        <span>{label}</span>
        <input
          type="number"
          className="devm-settings-input"
          min={1}
          max={1440}
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(Number(event.target.value))}
          style={{ width: '88px', minHeight: '28px', textAlign: 'right' }}
        />
      </div>
    </div>
  )
}
