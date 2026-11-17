import React from 'react'

// Poros mark: white hexagon outline + three long cyan arrows radiating from the
// centre THROUGH the hexagon vertices (up, lower-left, lower-right). Navy badge.
export default function PorosMark({ size = 36, title = 'Poros' }) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" role="img" aria-label={title}
         style={{ display: 'block', flexShrink: 0 }}>
      <rect x="0" y="0" width="100" height="100" rx="22" fill="#12263e" />
      {/* hexagon */}
      <path d="M50 33 L69 44 L69 66 L50 77 L31 66 L31 44 Z"
            fill="none" stroke="#ffffff" strokeWidth="4.5" strokeLinejoin="round" />
      {/* three long arrows from the centre, passing through the hexagon */}
      <g fill="none" stroke="#2ea9e6" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M50 55 L50 10 M43 17 L50 10 L57 17" />
        <path d="M50 55 L14 76 M23 76 L14 76 L18.5 68" />
        <path d="M50 55 L86 76 M77 76 L86 76 L81.5 68" />
      </g>
    </svg>
  )
}
