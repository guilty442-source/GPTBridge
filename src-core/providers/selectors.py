CHATGPT_SELECTORS = {
    "input": [
        '[data-testid="prompt-textarea"]',
        '#prompt-textarea',
        'div[contenteditable="true"][data-testid="prompt-textarea"]',
        'div[role="textbox"][contenteditable="true"]',
        'textarea'
    ],
    "send": [
        'button[data-testid="send-button"]:not([disabled])',
        '[data-testid="send-button"]:not([disabled])',
        'button[data-testid="fruitjuice-send-button"]:not([disabled])',
        'button[aria-label*="Send prompt"]:not([disabled])',
        'button[aria-label*="Send message"]:not([disabled])',
        'button[aria-label*="Send"]:not([disabled])',
        'button[aria-label*="\u9001\u51fa"]:not([disabled])',
        'button[aria-label*="\u50b3\u9001"]:not([disabled])'
    ],
    "response": [
        '[data-message-author-role="assistant"]',
        'div.markdown.prose',
        'article'
    ],
    "login_check": [
        '[data-testid="prompt-textarea"]',
        '#prompt-textarea',
        '[contenteditable="true"]',
        'textarea'
    ]
}


GEMINI_SELECTORS = {
    "input": [
        'rich-textarea div[contenteditable="true"]',
        'rich-textarea div[role="textbox"]',
        'div[role="textbox"][contenteditable="true"]',
        'div[contenteditable="true"]',
        '[aria-label*="Enter a prompt"]',
        '[aria-label*="\u8f38\u5165\u63d0\u793a"]',
        'textarea'
    ],
    "send": [
        'button[aria-label*="Send message"]:not([disabled])',
        'button[aria-label*="Send"]:not([disabled])',
        'button:has(mat-icon[path*="send"]):not([disabled])',
        'button[aria-label*="\u50b3\u9001"]:not([disabled])',
        'button[aria-label*="\u9001\u51fa"]:not([disabled])',
        '.send-button-container button:not([disabled])'
    ],
    "response": [
        'model-response',
        '.model-response-text',
        'div.message-content',
        '[data-test-id="response-content"]'
    ],
    "login_check": [
        'rich-textarea',
        'div[contenteditable="true"]',
        '[aria-label*="Enter a prompt"]',
        '[aria-label*="\u8f38\u5165\u63d0\u793a"]',
        'main'
    ]
}
