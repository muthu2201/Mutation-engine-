// colloid-loadgen: an open-loop HTTP load generator over a Unix socket.
//
// Why a dedicated tool instead of wrk/k6/hyperfine?
//
//  1. Open-loop scheduling with coordinated-omission correction. Every request has a
//     *scheduled* send time (the evaluator generates Poisson arrivals with a seed). Latency
//     is measured from the scheduled time, not from when a connection happened to become
//     free, so a server that stalls cannot hide the queueing delay it caused (the classic
//     closed-loop benchmarking bug).
//  2. Exact, window-aligned CPU accounting. The tool reads the cgroup cpuacct.usage files of
//     the service and database process trees at fixed window boundaries on its own clock,
//     so "CPU per request" per window is computed from the same timeline as the requests.
//  3. Spot checks under load. Requests flagged by the evaluator (a random, hidden subset)
//     have their full response bodies recorded, so the evaluator can verify that responses
//     produced *under load* are identical to the reference - a candidate cannot behave
//     correctly for the slow correctness oracle and cheat when it is being timed.
//  4. Unix sockets: the candidate runs in an empty network namespace and only exposes a
//     Unix socket.
//
// Input: JSON lines {"t": offset_us, "m": "GET", "p": "/path", "b": "body", "s": 1}
// Output (CSV-ish, one record per line):
//
//	r,<idx>,<sched_us>,<start_us>,<end_us>,<status>,<bytes>
//	c,<t_us>,<cpu_ns_file1>,<cpu_ns_file2>,...
//	b,<idx>,<base64 body>
//	s,<json summary>
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type request struct {
	T int64  `json:"t"`
	M string `json:"m"`
	P string `json:"p"`
	B string `json:"b"`
	S int    `json:"s"`
}

type result struct {
	sched, start, end int64
	status, bytes     int
	body              []byte
}

// parseCPU returns cumulative CPU nanoseconds from one counter file. Three formats:
// a cgroup v1 cpuacct.usage (a single integer, ns), a cgroup v2 cpu.stat ("usage_usec N"
// among other lines, microseconds), or a portable counter file written by the engine's
// psutil sampler (a single integer, ns). Unreadable or malformed input returns -1.
func parseCPU(name string, data []byte) int64 {
	text := strings.TrimSpace(string(data))
	if filepath.Base(name) == "cpu.stat" {
		for _, line := range strings.Split(text, "\n") {
			fields := strings.Fields(line)
			if len(fields) == 2 && fields[0] == "usage_usec" {
				v, err := strconv.ParseInt(fields[1], 10, 64)
				if err != nil {
					return -1
				}
				return v * 1000
			}
		}
		return -1
	}
	v, err := strconv.ParseInt(text, 10, 64)
	if err != nil {
		return -1
	}
	return v
}

// readFileRetry reads a counter file, retrying briefly: on Windows the engine's portable CPU
// counter is replaced atomically, and a read that collides with the replace fails for a moment.
func readFileRetry(f string) ([]byte, error) {
	var err error
	for attempt := 0; attempt < 50; attempt++ {
		var data []byte
		if data, err = os.ReadFile(f); err == nil {
			return data, nil
		}
		if os.IsNotExist(err) {
			return nil, err
		}
		time.Sleep(500 * time.Microsecond)
	}
	return nil, err
}

func readCPU(files []string) []int64 {
	out := make([]int64, len(files))
	for i, f := range files {
		data, err := readFileRetry(f)
		if err != nil {
			out[i] = -1
			continue
		}
		out[i] = parseCPU(f, data)
	}
	return out
}

func main() {
	socket := flag.String("socket", "", "unix socket path of the service")
	reqPath := flag.String("requests", "", "JSON-lines request schedule")
	conns := flag.Int("conns", 16, "max concurrent connections")
	timeout := flag.Duration("timeout", 10*time.Second, "per-request timeout")
	windowMs := flag.Int("window-ms", 500, "CPU sampling window")
	cpuFiles := flag.String("cpu-files", "", "comma-separated cpuacct.usage files")
	outPath := flag.String("out", "-", "output file")
	flag.Parse()
	if *socket == "" || *reqPath == "" {
		fmt.Fprintln(os.Stderr, "usage: loadgen -socket S -requests R [-conns N] [-cpu-files a,b] [-out F]")
		os.Exit(2)
	}

	fh, err := os.Open(*reqPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	var reqs []request
	sc := bufio.NewScanner(fh)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	for sc.Scan() {
		var r request
		if err := json.Unmarshal(sc.Bytes(), &r); err != nil {
			fmt.Fprintln(os.Stderr, "bad request line:", err)
			os.Exit(2)
		}
		reqs = append(reqs, r)
	}
	fh.Close()

	var files []string
	if *cpuFiles != "" {
		files = strings.Split(*cpuFiles, ",")
	}

	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", *socket)
		},
		MaxConnsPerHost:     *conns,
		MaxIdleConnsPerHost: *conns,
		MaxIdleConns:        *conns,
		DisableCompression:  true,
		IdleConnTimeout:     60 * time.Second,
	}
	client := &http.Client{Transport: transport, Timeout: *timeout}

	results := make([]result, len(reqs))
	jobs := make(chan int, len(reqs))
	var wg sync.WaitGroup
	var inflight, maxInflight int64
	start := time.Now()
	since := func() int64 { return time.Since(start).Microseconds() }

	for w := 0; w < *conns; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range jobs {
				r := reqs[i]
				cur := atomic.AddInt64(&inflight, 1)
				for {
					old := atomic.LoadInt64(&maxInflight)
					if cur <= old || atomic.CompareAndSwapInt64(&maxInflight, old, cur) {
						break
					}
				}
				var body io.Reader
				if r.B != "" {
					body = strings.NewReader(r.B)
				}
				req, _ := http.NewRequest(r.M, "http://candidate"+r.P, body)
				if r.B != "" {
					req.Header.Set("Content-Type", "application/json")
				}
				res := result{sched: r.T, start: since()}
				resp, err := client.Do(req)
				if err != nil {
					res.status = -1
				} else {
					var buf bytes.Buffer
					n, _ := io.Copy(&buf, resp.Body)
					resp.Body.Close()
					res.status = resp.StatusCode
					res.bytes = int(n)
					if r.S == 1 {
						res.body = buf.Bytes()
					}
				}
				res.end = since()
				results[i] = res
				atomic.AddInt64(&inflight, -1)
			}
		}()
	}

	// CPU sampler on the load generator's own clock.
	type cpuSample struct {
		t    int64
		vals []int64
	}
	var samples []cpuSample
	var smu sync.Mutex
	stop := make(chan struct{})
	samplerDone := make(chan struct{})
	if len(files) > 0 {
		samples = append(samples, cpuSample{0, readCPU(files)})
		go func() {
			defer close(samplerDone)
			tick := time.NewTicker(time.Duration(*windowMs) * time.Millisecond)
			defer tick.Stop()
			for {
				select {
				case <-stop:
					return
				case <-tick.C:
					v := readCPU(files)
					smu.Lock()
					samples = append(samples, cpuSample{since(), v})
					smu.Unlock()
				}
			}
		}()
	} else {
		close(samplerDone)
	}

	// Dispatcher: release each request at its scheduled time (open loop).
	for i, r := range reqs {
		target := start.Add(time.Duration(r.T) * time.Microsecond)
		if d := time.Until(target); d > 0 {
			if d > 200*time.Microsecond {
				time.Sleep(d - 100*time.Microsecond)
			}
			for time.Now().Before(target) {
			}
		}
		jobs <- i
	}
	close(jobs)
	wg.Wait()
	close(stop)
	<-samplerDone
	if len(files) > 0 {
		samples = append(samples, cpuSample{since(), readCPU(files)})
	}
	elapsed := since()

	var out io.Writer = os.Stdout
	if *outPath != "-" {
		f, err := os.Create(*outPath)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		defer f.Close()
		out = f
	}
	bw := bufio.NewWriterSize(out, 1<<20)
	errors := 0
	for i, r := range results {
		fmt.Fprintf(bw, "r,%d,%d,%d,%d,%d,%d\n", i, r.sched, r.start, r.end, r.status, r.bytes)
		if r.status < 200 || r.status >= 300 {
			errors++
		}
		if r.body != nil {
			fmt.Fprintf(bw, "b,%d,%s\n", i, base64.StdEncoding.EncodeToString(r.body))
		}
	}
	for _, s := range samples {
		parts := make([]string, len(s.vals))
		for j, v := range s.vals {
			parts[j] = strconv.FormatInt(v, 10)
		}
		fmt.Fprintf(bw, "c,%d,%s\n", s.t, strings.Join(parts, ","))
	}
	summary, _ := json.Marshal(map[string]any{
		"requests": len(reqs), "errors": errors, "elapsed_us": elapsed, "max_inflight": maxInflight, "conns": *conns,
	})
	fmt.Fprintf(bw, "s,%s\n", summary)
	bw.Flush()
}
