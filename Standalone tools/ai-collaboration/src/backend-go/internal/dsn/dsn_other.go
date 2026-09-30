//go:build !windows

package dsn

import (
	"errors"
	"fmt"
	"os"
	"strings"
)

const credmanScheme = "credman:"

var errUnavailable = errors.New("runtime DSN unavailable")

// Resolve on non-Windows hosts: env only; credman: fails closed.
func Resolve() (string, error) {
	raw := strings.TrimSpace(os.Getenv("GPTBRIDGE_POSTGRES_DSN"))
	if raw == "" {
		return "", fmt.Errorf("%w: GPTBRIDGE_POSTGRES_DSN unset", errUnavailable)
	}
	if strings.HasPrefix(raw, credmanScheme) {
		return "", fmt.Errorf("%w: credman resolution unsupported on this platform", errUnavailable)
	}
	return raw, nil
}
