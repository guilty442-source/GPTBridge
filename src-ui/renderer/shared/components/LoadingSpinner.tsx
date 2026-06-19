import React from 'react'

export const LoadingSpinner: React.FC = () => (
  <svg
    width="1em"
    height="1em"
    viewBox="0 0 24 24"
    style={{ animation: 'gptb-spin 1s linear infinite' }}
    xmlns="http://www.w3.org/2000/svg"
  >
    <style>{`@keyframes gptb-spin { 100% { transform: rotate(360deg); } }`}</style>
    <circle
      cx="12"
      cy="12"
      r="10"
      stroke="currentColor"
      strokeWidth="3"
      fill="none"
      opacity="0.25"
    />
    <path
      d="M12 2a10 10 0 0 1 10 10"
      stroke="currentColor"
      strokeWidth="3"
      fill="none"
      strokeLinecap="round"
    />
  </svg>
)
