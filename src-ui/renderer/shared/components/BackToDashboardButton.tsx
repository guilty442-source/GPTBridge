type BackToDashboardButtonProps = {
  onBack?: () => void
}

export function BackToDashboardButton({ onBack }: BackToDashboardButtonProps) {
  const handleBack = () => {
    if (onBack) {
      onBack()
      return
    }

    window.dispatchEvent(new CustomEvent('navigate', { detail: 'dashboard' }))
  }

  return (
    <button type="button" onClick={handleBack}>
      Back to Dashboard
    </button>
  )
}
