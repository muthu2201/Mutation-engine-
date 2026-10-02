package main

import "testing"

func TestParseCPU(t *testing.T) {
	cases := []struct {
		name, data string
		want       int64
	}{
		{"/sys/fs/cgroup/cpuacct/colloid/x/cpuacct.usage", "123456789\n", 123456789},
		{"/sys/fs/cgroup/colloid/x/cpu.stat", "usage_usec 2500\nuser_usec 2000\nsystem_usec 500\n", 2500000},
		{"/home/u/.colloid/state/sandbox-logs/x.cpu_ns", "42", 42},
		{"/sys/fs/cgroup/colloid/x/cpu.stat", "user_usec 1\n", -1},
		{"/x/cpuacct.usage", "not-a-number", -1},
	}
	for _, c := range cases {
		if got := parseCPU(c.name, []byte(c.data)); got != c.want {
			t.Errorf("parseCPU(%q) = %d, want %d", c.name, got, c.want)
		}
	}
}
