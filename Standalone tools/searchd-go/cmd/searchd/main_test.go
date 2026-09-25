package main

import "testing"

func TestLoopbackOnly(t *testing.T) {
	for _, ok := range []string{"127.0.0.1:8091", "localhost:8091", "[::1]:8091"} {
		if err := loopbackOnly(ok); err != nil {
			t.Errorf("loopback addr %q rejected: %v", ok, err)
		}
	}
	for _, bad := range []string{"0.0.0.0:8091", ":8091", "192.168.1.5:80", "example.com:80"} {
		if err := loopbackOnly(bad); err == nil {
			t.Errorf("non-loopback addr %q accepted", bad)
		}
	}
}
