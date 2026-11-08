package main

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRecoverySeedPublication(t *testing.T) {
	dir := t.TempDir()
	final := filepath.Join(dir, "database.db")
	if err := publishRecoverySeed(strings.NewReader("verified seed"), final); err != nil {
		t.Fatal(err)
	}
	// A transport failure after some bytes must preserve the previous seed.
	err := publishRecoverySeed(io.MultiReader(strings.NewReader("partial"), brokenSeedReader{}), final)
	if err == nil {
		t.Fatal("partial seed published despite source failure")
	}
	data, err := os.ReadFile(final)
	if err != nil || string(data) != "verified seed" {
		t.Fatalf("published seed changed after failed copy: %q, %v", data, err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) != 1 {
		t.Fatalf("temporary seeds leaked: %v, %v", entries, err)
	}
	if err := publishRecoverySeed(strings.NewReader("replacement seed"), final); err != nil {
		t.Fatal(err)
	}
	data, err = os.ReadFile(final)
	if err != nil || string(data) != "replacement seed" {
		t.Fatalf("replacement not published: %q, %v", data, err)
	}
}

type brokenSeedReader struct{}

func (brokenSeedReader) Read([]byte) (int, error) { return 0, errors.New("source read failed") }

type memoryStore struct {
	v          Volume
	beforeEdit func()
}

func (s *memoryStore) Get(context.Context, string) (Volume, error)        { return s.v, nil }
func (s *memoryStore) Create(_ context.Context, v Volume) (Volume, error) { s.v = v; return v, nil }
func (s *memoryStore) Edit(_ context.Context, _ string, edit func(*Volume) error) (Volume, error) {
	if s.beforeEdit != nil {
		s.beforeEdit()
	}
	next := s.v
	if next.Recovery != nil {
		record := *next.Recovery
		next.Recovery = &record
	}
	if err := edit(&next); err != nil {
		return s.v, err
	}
	s.v = next
	return next, nil
}

type fakeCluster struct {
	node            FenceNode
	events          *[]string
	listError       error
	quarantineError error
	replace         func()
}

func (c *fakeCluster) Nodes(context.Context) ([]FenceNode, error) {
	if c.replace != nil {
		c.replace()
	}
	return []FenceNode{c.node}, c.listError
}
func (c *fakeCluster) Volumes(context.Context) ([]Volume, error) { return nil, nil }
func (c *fakeCluster) Quarantine(context.Context, FenceNode) error {
	*c.events = append(*c.events, "quarantine")
	return c.quarantineError
}

type fakeFencer struct {
	node       FenceNode
	now        time.Time
	events     *[]string
	err        error
	wrongProof bool
}

func (f *fakeFencer) PowerOff(_ context.Context, n FenceNode, id string) (PowerOffProof, error) {
	*f.events = append(*f.events, "power-off")
	if f.err != nil {
		return PowerOffProof{}, f.err
	}
	uid := n.UID
	if f.wrongProof {
		uid = "different-node"
	}
	return PowerOffProof{NodeUID: uid, MachineID: "vm-identity", OperationID: id, ObservedAt: f.now}, nil
}
func fixture(t *testing.T) (*RecoveryController, *memoryStore, *fakeCluster, *fakeFencer, *[]string) {
	t.Helper()
	now := time.Date(2026, 1, 1, 0, 10, 0, 0, time.UTC)
	events := []string{}
	node := FenceNode{Name: "worker-a", UID: "node-a", Ready: "Unknown", Transition: now.Add(-5 * time.Minute).Format(time.RFC3339)}
	store := &memoryStore{v: Volume{ID: "v-0123456789abcdef0123456789abcdef", Database: "database.db", Owner: node.Name, Epoch: 4, Prepared: true, RecoveryPolicy: "latest-replica"}}
	cluster := &fakeCluster{node: node, events: &events}
	fencer := &fakeFencer{now: now, events: &events}
	controller := &RecoveryController{Store: store, Cluster: cluster, Fencer: fencer, Grace: time.Minute, Now: func() time.Time { return now },
		Prepare: func(_ context.Context, v Volume, r RecoveryRecord) (RecoveredReplica, error) {
			events = append(events, "verify-replica")
			return RecoveredReplica{Path: v.ID + "/recoveries/" + r.ID + "/database.db", Digest: strings.Repeat("a", 64)}, nil
		}}
	return controller, store, cluster, fencer, &events
}
func TestRecoveryPowersOffBeforeQuarantineAndOwnershipRelease(t *testing.T) {
	r, s, c, _, events := fixture(t)
	if err := r.reconcile(context.Background(), s.v, c.node); err != nil {
		t.Fatal(err)
	}
	if strings.Join(*events, ",") != "power-off,verify-replica,quarantine" {
		t.Fatal(*events)
	}
	if s.v.Owner != "" || s.v.Clean || !s.v.Initialized || s.v.RecoverySeed == "" || s.v.Recovery.Phase != "released" {
		t.Fatal(s.v)
	}
	if s.v.ReplicaDirectory == "replica" || s.v.ReplicaDirectory == "" {
		t.Fatal("old writer can reach new history")
	}
	if err := acquire(&s.v, "worker-b"); err != nil {
		t.Fatal(err)
	}
	if s.v.Epoch != 5 {
		t.Fatal(s.v.Epoch)
	}
}
func TestNetworkPartitionWithoutPowerProofCannotRecover(t *testing.T) {
	r, s, c, f, events := fixture(t)
	f.err = errors.New("fencing authority unavailable")
	if err := r.reconcile(context.Background(), s.v, c.node); err == nil {
		t.Fatal("unconfirmed fencing accepted")
	}
	if s.v.Owner != "worker-a" || s.v.RecoverySeed != "" || strings.Join(*events, ",") != "power-off" {
		t.Fatal(s.v, *events)
	}
	if err := acquire(&s.v, "worker-b"); err == nil {
		t.Fatal("split brain permitted")
	}
}
func TestBrokenReplicaLeavesFailedWriterFencedAndOwnershipLocked(t *testing.T) {
	r, s, c, _, events := fixture(t)
	r.Prepare = func(context.Context, Volume, RecoveryRecord) (RecoveredReplica, error) {
		return RecoveredReplica{}, errors.New("replica missing/corrupt")
	}
	if err := r.reconcile(context.Background(), s.v, c.node); err == nil {
		t.Fatal("missing replica accepted")
	}
	if s.v.Owner != "worker-a" || s.v.Initialized || s.v.Recovery.Phase != "fenced" || strings.Join(*events, ",") != "power-off" {
		t.Fatal(s.v, *events)
	}
}
func TestRecoveredOrReplacedNodeIsNeverFenced(t *testing.T) {
	for _, change := range []string{"ready", "uid", "incident"} {
		t.Run(change, func(t *testing.T) {
			r, s, c, _, events := fixture(t)
			original := c.node
			c.replace = func() {
				switch change {
				case "ready":
					c.node.Ready = "True"
				case "uid":
					c.node.UID = "replacement"
				case "incident":
					c.node.Transition = r.Now().Format(time.RFC3339)
				}
			}
			if err := r.reconcile(context.Background(), s.v, original); err == nil {
				t.Fatal("health change ignored")
			}
			if len(*events) != 0 || s.v.Owner != "worker-a" {
				t.Fatal(*events, s.v)
			}
		})
	}
}
func TestDefaultAndStrictVolumesNeverAutomaticallyRecover(t *testing.T) {
	for _, policy := range []string{"", "manual", "zero-loss"} {
		t.Run(policy, func(t *testing.T) {
			r, s, c, _, events := fixture(t)
			s.v.RecoveryPolicy = policy
			if err := r.reconcile(context.Background(), s.v, c.node); err != nil {
				t.Fatal(err)
			}
			if len(*events) != 0 || s.v.Owner != "worker-a" {
				t.Fatal("unexpected automatic recovery")
			}
		})
	}
}
func TestQuarantineFailureNeverUnlocksVolume(t *testing.T) {
	r, s, c, _, _ := fixture(t)
	c.quarantineError = errors.New("API unavailable")
	if err := r.reconcile(context.Background(), s.v, c.node); err == nil {
		t.Fatal("quarantine failure ignored")
	}
	if s.v.Owner != "worker-a" {
		t.Fatal("ownership released before quarantine")
	}
}
func TestMismatchedPowerProofNeverRestoresOrUnlocks(t *testing.T) {
	r, s, c, f, events := fixture(t)
	f.wrongProof = true
	if err := r.reconcile(context.Background(), s.v, c.node); err == nil {
		t.Fatal("mismatched proof accepted")
	}
	if strings.Join(*events, ",") != "power-off" || s.v.Owner != "worker-a" {
		t.Fatal(*events, s.v)
	}
}
func TestFutureAndFreshUnhealthyTransitionsDoNotTriggerFencing(t *testing.T) {
	r, s, c, _, events := fixture(t)
	for _, offset := range []time.Duration{time.Minute, -time.Second} {
		node := c.node
		node.Transition = r.Now().Add(offset).Format(time.RFC3339)
		if err := r.reconcile(context.Background(), s.v, node); err != nil {
			t.Fatal(err)
		}
	}
	if len(*events) != 0 {
		t.Fatal(*events)
	}
}
