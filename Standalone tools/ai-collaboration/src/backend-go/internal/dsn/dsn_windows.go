//go:build windows

// Package dsn resolves the governed PostgreSQL runtime DSN fail-closed:
// GPTBRIDGE_POSTGRES_DSN env → HKCU\Environment → credman: indirection
// through the Windows credential store (CredReadW). No defaults.
package dsn

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows/registry"
)

const credmanScheme = "credman:"

var (
	advapi32       = syscall.NewLazyDLL("advapi32.dll")
	procCredReadW  = advapi32.NewProc("CredReadW")
	procCredFree   = advapi32.NewProc("CredFree")
	errUnavailable = errors.New("runtime DSN unavailable")
)

type fileTime struct {
	Low, High uint32
}

type credentialW struct {
	Flags, CredType uint32
	TargetName      *uint16
	Comment         *uint16
	LastWritten     fileTime
	BlobSize        uint32
	Blob            *byte
	Persist         uint32
	AttrCount       uint32
	Attributes      uintptr
	TargetAlias     *uint16
	UserName        *uint16
}

// Resolve returns the runtime DSN or fails closed.
func Resolve() (string, error) {
	raw := strings.TrimSpace(os.Getenv("GPTBRIDGE_POSTGRES_DSN"))
	if raw == "" {
		raw = strings.TrimSpace(readUserEnv("GPTBRIDGE_POSTGRES_DSN"))
	}
	if raw == "" {
		return "", fmt.Errorf("%w: GPTBRIDGE_POSTGRES_DSN unset", errUnavailable)
	}
	if target, ok := strings.CutPrefix(raw, credmanScheme); ok {
		target = strings.TrimSpace(target)
		if target == "" {
			return "", fmt.Errorf("%w: empty credman target", errUnavailable)
		}
		return credmanRead(target)
	}
	return raw, nil
}

// readUserEnv reads the Windows user environment (HKCU\Environment) so a
// governed tool process still sees the authoritative binding even though
// the launcher scrubs it from the inherited process environment.
func readUserEnv(name string) string {
	key, err := registry.OpenKey(registry.CURRENT_USER, "Environment", registry.QUERY_VALUE)
	if err != nil {
		return ""
	}
	defer key.Close()
	value, _, err := key.GetStringValue(name)
	if err != nil {
		return ""
	}
	return value
}

func credmanRead(target string) (string, error) {
	const credTypeGeneric = 1
	t, err := syscall.UTF16PtrFromString(target)
	if err != nil {
		return "", fmt.Errorf("%w: credman target invalid", errUnavailable)
	}
	var cred *credentialW
	rc, _, _ := procCredReadW.Call(
		uintptr(unsafe.Pointer(t)), credTypeGeneric, 0,
		uintptr(unsafe.Pointer(&cred)))
	if rc == 0 || cred == nil {
		return "", fmt.Errorf("%w: credman:%s not found", errUnavailable, target)
	}
	defer procCredFree.Call(uintptr(unsafe.Pointer(cred)))
	if cred.Blob == nil || cred.BlobSize == 0 {
		return "", fmt.Errorf("%w: credman:%s empty secret", errUnavailable, target)
	}
	blob := unsafe.Slice(cred.Blob, cred.BlobSize)
	out := strings.TrimSpace(string(blob))
	if out == "" {
		return "", fmt.Errorf("%w: credman:%s empty secret", errUnavailable, target)
	}
	return out, nil
}
