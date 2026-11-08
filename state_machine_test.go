package main

import (
	"context"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// Exercise long mixed histories against an independent ownership model. Failed
// requests must not change ownership or the generation visible to a writer.
func TestOwnershipStateMachine(t *testing.T) {
	for _, seed := range []int64{1, 42, 2026, 987654321} {
		t.Run(fmt.Sprint(seed), func(t *testing.T) {
			rng := rand.New(rand.NewSource(seed))
			var trace []string
			defer func() {
				if t.Failed() {
					t.Logf("seed=%d operation trace:\n%s", seed, strings.Join(trace, "\n"))
				}
			}()
			v := Volume{}
			owner := ""
			var epoch int64
			prepared, clean := false, false
			for step := 0; step < 2000; step++ {
				node := []string{"node-a", "node-b", "node-c"}[rng.Intn(3)]
				op := rng.Intn(5)
				trace = append(trace, fmt.Sprintf("%d: op=%d node=%s", step, op, node))
				switch op {
				case 0, 1:
					err := acquire(&v, node)
					allowed := owner == "" || owner == node
					if (err == nil) != allowed {
						t.Fatalf("step %d acquire %s: owner=%s err=%v", step, node, owner, err)
					}
					if allowed && owner == "" {
						owner = node
						epoch++
						prepared, clean = false, false
					}
				case 2:
					err := release(&v, node)
					allowed := owner == "" || (owner == node && (!prepared || clean))
					if (err == nil) != allowed {
						t.Fatalf("step %d release %s: owner=%s err=%v", step, node, owner, err)
					}
					if allowed {
						owner = ""
					}
				case 3:
					if owner != "" {
						prepared, clean = true, false
						v.Prepared, v.Clean = true, false
					}
				case 4:
					if owner != "" && prepared {
						clean = true
						v.Clean = true
					}
				}
				if v.Owner != owner || v.Epoch != epoch || v.Prepared != prepared || v.Clean != clean {
					t.Fatalf("step %d: got owner=%s epoch=%d prepared=%v clean=%v; want %s %d %v %v", step, v.Owner, v.Epoch, v.Prepared, v.Clean, owner, epoch, prepared, clean)
				}
			}
		})
	}
}

// Real concurrent clients contend through resource-version CAS, as they do
// against Kubernetes. Exactly one can acquire an initially unowned volume.
func TestConcurrentOwnershipCAS(t *testing.T) {
	var mu sync.Mutex
	v := Volume{ID: "v-0123456789abcdef0123456789abcdef"}
	version := 1
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch r.Method {
		case http.MethodGet:
			data, _ := json.Marshal(v)
			cm := configMap{Data: map[string]string{"volume.json": string(data)}}
			cm.Metadata.ResourceVersion = fmt.Sprint(version)
			_ = json.NewEncoder(w).Encode(cm)
		case http.MethodPut:
			var cm configMap
			if err := json.NewDecoder(r.Body).Decode(&cm); err != nil {
				t.Error(err)
				w.WriteHeader(400)
				return
			}
			if cm.Metadata.ResourceVersion != fmt.Sprint(version) {
				w.WriteHeader(http.StatusConflict)
				return
			}
			if err := json.Unmarshal([]byte(cm.Data["volume.json"]), &v); err != nil {
				t.Error(err)
				w.WriteHeader(400)
				return
			}
			version++
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	defer server.Close()
	api := API{client: server.Client(), base: server.URL, namespace: "test"}
	id := v.ID
	const clients = 12
	start := make(chan struct{})
	results := make(chan string, clients)
	var wg sync.WaitGroup
	for i := 0; i < clients; i++ {
		node := fmt.Sprintf("node-%d", i)
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			if _, err := api.Edit(context.Background(), id, func(current *Volume) error { return acquire(current, node) }); err == nil {
				results <- node
			}
		}()
	}
	close(start)
	wg.Wait()
	close(results)
	winners := []string{}
	for node := range results {
		winners = append(winners, node)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(winners) != 1 || v.Owner != winners[0] || v.Epoch != 1 {
		t.Fatalf("winners=%v volume=%+v", winners, v)
	}
}

// A failed attempt can be retried, but the next owner must never acquire the
// volume before power-off, a verified seed and quarantine have all succeeded.
func TestRecoveryRetriesKeepUncleanOwnerLocked(t *testing.T) {
	for _, failure := range []string{"fencing", "replica", "quarantine"} {
		t.Run(failure, func(t *testing.T) {
			r, s, c, f, _ := fixture(t)
			prepare := r.Prepare
			switch failure {
			case "fencing":
				f.err = fmt.Errorf("authority unavailable")
			case "replica":
				r.Prepare = func(context.Context, Volume, RecoveryRecord) (RecoveredReplica, error) {
					return RecoveredReplica{}, fmt.Errorf("bad replica")
				}
			case "quarantine":
				c.quarantineError = fmt.Errorf("API unavailable")
			}
			if err := r.reconcile(context.Background(), s.v, c.node); err == nil {
				t.Fatal("failed stage accepted")
			}
			if s.v.Owner != c.node.Name || s.v.Epoch != 4 {
				t.Fatalf("failed recovery changed ownership: %+v", s.v)
			}
			if err := acquire(&s.v, "worker-b"); err == nil {
				t.Fatal("unclean owner displaced after failed recovery")
			}
			f.err = nil
			r.Prepare = prepare
			c.quarantineError = nil
			if err := r.reconcile(context.Background(), s.v, c.node); err != nil {
				t.Fatal(err)
			}
			if s.v.Owner != "" || s.v.RecoverySeed == "" || s.v.Recovery == nil || s.v.Recovery.Phase != "released" {
				t.Fatalf("incomplete recovery: %+v", s.v)
			}
			if err := acquire(&s.v, "worker-b"); err != nil {
				t.Fatal(err)
			}
			if s.v.Owner != "worker-b" || s.v.Epoch != 5 {
				t.Fatalf("takeover: %+v", s.v)
			}
		})
	}
}
