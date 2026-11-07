package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// PowerOffProof is evidence from an independent fencing authority. Kubernetes
// NodeReady, a stale Lease and an unreachable TCP port are never such evidence.
type PowerOffProof struct {
	NodeUID     string    `json:"nodeUID"`
	MachineID   string    `json:"machineID"`
	OperationID string    `json:"operationID"`
	ObservedAt  time.Time `json:"observedAt"`
}
type RecoveryRecord struct {
	ID      string        `json:"id"`
	Owner   string        `json:"owner"`
	Epoch   int64         `json:"epoch"`
	NodeUID string        `json:"nodeUID"`
	Phase   string        `json:"phase"`
	Proof   PowerOffProof `json:"proof"`
	Seed    string        `json:"seed,omitempty"`
	Digest  string        `json:"digest,omitempty"`
}
type FenceNode struct {
	Name       string
	UID        string
	Transition string
	Ready      string
}
type Fencer interface {
	PowerOff(context.Context, FenceNode, string) (PowerOffProof, error)
}
type RecoveryCluster interface {
	Nodes(context.Context) ([]FenceNode, error)
	Volumes(context.Context) ([]Volume, error)
	Quarantine(context.Context, FenceNode) error
}
type RecoveredReplica struct{ Path, Digest string }
type RecoveryController struct {
	Store   Store
	Cluster RecoveryCluster
	Fencer  Fencer
	Grace   time.Duration
	Now     func() time.Time
	Prepare func(context.Context, Volume, RecoveryRecord) (RecoveredReplica, error)
}

func normalizedPolicy(s string) string {
	if s == "" {
		return "manual"
	}
	return s
}
func replicaDirectory(v Volume) string {
	if v.ReplicaDirectory == "" {
		return "replica"
	}
	return v.ReplicaDirectory
}
func recoveryID(v Volume, node FenceNode) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s/%d/%s/%s", v.ID, v.Epoch, node.UID, node.Transition)))
	return fmt.Sprintf("r-%x", sum[:16])
}
func sameIncident(a, b FenceNode) bool {
	return a.Name == b.Name && a.UID == b.UID && a.Transition == b.Transition && a.Ready == b.Ready
}
func recoverable(v Volume, node FenceNode, now time.Time, grace time.Duration) bool {
	if normalizedPolicy(v.RecoveryPolicy) != "latest-replica" || v.Deleted || v.Owner != node.Name || !v.Prepared || v.Clean {
		return false
	}
	if node.UID == "" || (node.Ready != "False" && node.Ready != "Unknown") {
		return false
	}
	transition, err := time.Parse(time.RFC3339, node.Transition)
	return err == nil && !now.Before(transition) && now.Sub(transition) >= grace
}
func finalizeRecovery(v *Volume, record RecoveryRecord, seed RecoveredReplica) error {
	if v.Owner != record.Owner || v.Epoch != record.Epoch || v.Deleted || v.Clean || !v.Prepared || normalizedPolicy(v.RecoveryPolicy) != "latest-replica" {
		return fmt.Errorf("volume ownership/policy changed during recovery")
	}
	if v.Recovery == nil || v.Recovery.ID != record.ID || record.Phase != "verified" || record.Proof.NodeUID != record.NodeUID || record.Proof.OperationID != record.ID || record.Proof.MachineID == "" || record.Proof.ObservedAt.IsZero() || seed.Path == "" || seed.Digest == "" {
		return fmt.Errorf("verified fenced recovery evidence required")
	}
	if !strings.HasPrefix(seed.Path, v.ID+"/recoveries/"+record.ID+"/") || filepath.Clean(seed.Path) != seed.Path {
		return fmt.Errorf("invalid recovered seed path")
	}
	v.Owner = ""
	v.Prepared = false
	v.Clean = false // This was recovery, not a clean final flush of the failed writer.
	v.Initialized = true
	v.ReplicaDirectory = "replica-" + record.ID // Old agents cannot publish into the new history.
	v.RecoverySeed = seed.Path
	v.SeedDigest = seed.Digest
	v.SeedClaimedEpoch = 0
	record.Phase = "released"
	record.Seed = seed.Path
	record.Digest = seed.Digest
	v.Recovery = &record
	return nil
}
func (r *RecoveryController) reconcile(ctx context.Context, v Volume, node FenceNode) error {
	if !recoverable(v, node, r.Now(), r.Grace) {
		return nil
	}
	record := RecoveryRecord{ID: recoveryID(v, node), Owner: v.Owner, Epoch: v.Epoch, NodeUID: node.UID, Phase: "fencing"}
	// Persist intent first. Epoch and owner CAS guards protect against late
	// detach/retry callbacks and a second controller making a stale decision.
	_, err := r.Store.Edit(ctx, v.ID, func(current *Volume) error {
		if current.Owner != v.Owner || current.Epoch != v.Epoch || !recoverable(*current, node, r.Now(), r.Grace) {
			return fmt.Errorf("recovery intent is stale")
		}
		current.Recovery = &record
		return nil
	})
	if err != nil {
		return err
	}
	// Re-read health immediately before the external fencing side effect.
	nodes, err := r.Cluster.Nodes(ctx)
	if err != nil {
		return err
	}
	fresh := false
	for _, n := range nodes {
		if sameIncident(n, node) {
			fresh = true
			break
		}
	}
	if !fresh {
		return fmt.Errorf("node health/identity changed before fencing")
	}
	proof, err := r.Fencer.PowerOff(ctx, node, record.ID)
	if err != nil {
		return fmt.Errorf("power fence not confirmed: %w", err)
	}
	if proof.NodeUID != node.UID || proof.OperationID != record.ID || proof.MachineID == "" || proof.ObservedAt.IsZero() {
		return fmt.Errorf("fencer returned mismatched proof")
	}
	record.Proof = proof
	record.Phase = "fenced"
	_, err = r.Store.Edit(ctx, v.ID, func(current *Volume) error {
		if current.Owner != v.Owner || current.Epoch != v.Epoch {
			return fmt.Errorf("ownership changed while fencing")
		}
		current.Recovery = &record
		return nil
	})
	if err != nil {
		return err
	}
	seed, err := r.Prepare(ctx, v, record)
	if err != nil {
		return fmt.Errorf("replica recovery failed; owner stays locked: %w", err)
	}
	record.Phase = "verified"
	record.Seed = seed.Path
	record.Digest = seed.Digest
	// Only confirmed power-off permits this taint. It lets Kubernetes remove
	// orphaned Pods/attachments using its non-graceful shutdown workflow.
	if err = r.Cluster.Quarantine(ctx, node); err != nil {
		return err
	}
	_, err = r.Store.Edit(ctx, v.ID, func(current *Volume) error { return finalizeRecovery(current, record, seed) })
	if err == nil {
		log.Printf("recovered %s from fenced node %s; unpublished writes may be lost", v.ID, node.Name)
	}
	return err
}
func (r *RecoveryController) tick(ctx context.Context) error {
	nodes, err := r.Cluster.Nodes(ctx)
	if err != nil {
		return err
	}
	volumes, err := r.Cluster.Volumes(ctx)
	if err != nil {
		return err
	}
	for _, v := range volumes {
		for _, node := range nodes {
			if recoverable(v, node, r.Now(), r.Grace) {
				if err = r.reconcile(ctx, v, node); err != nil {
					log.Printf("recovery blocked for %s: %v", v.ID, err)
				}
			}
		}
	}
	return nil
}
func (r *RecoveryController) run(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		if err := r.tick(ctx); err != nil {
			log.Printf("recovery scan: %v", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

type kubeNode struct {
	Metadata struct {
		Name            string `json:"name"`
		UID             string `json:"uid"`
		ResourceVersion string `json:"resourceVersion"`
	} `json:"metadata"`
	Spec struct {
		Taints []map[string]any `json:"taints"`
	} `json:"spec"`
	Status struct {
		Conditions []struct{ Type, Status, LastTransitionTime string } `json:"conditions"`
	} `json:"status"`
}

func (n kubeNode) fenceNode() FenceNode {
	out := FenceNode{Name: n.Metadata.Name, UID: n.Metadata.UID}
	for _, condition := range n.Status.Conditions {
		if condition.Type == "Ready" {
			out.Ready = condition.Status
			out.Transition = condition.LastTransitionTime
		}
	}
	return out
}
func (a *API) Nodes(ctx context.Context) ([]FenceNode, error) {
	var list struct {
		Items []kubeNode `json:"items"`
	}
	err := a.request(ctx, "GET", "/api/v1/nodes", nil, &list)
	out := []FenceNode{}
	for _, n := range list.Items {
		out = append(out, n.fenceNode())
	}
	return out, err
}
func (a *API) Volumes(ctx context.Context) ([]Volume, error) {
	var list struct {
		Items []configMap `json:"items"`
	}
	err := a.request(ctx, "GET", a.path("")+"?labelSelector="+url.QueryEscape("sqlite.csi/volume=true"), nil, &list)
	if err != nil {
		return nil, err
	}
	out := []Volume{}
	for _, cm := range list.Items {
		var v Volume
		if err = json.Unmarshal([]byte(cm.Data["volume.json"]), &v); err != nil {
			return nil, err
		}
		if v.ID != cm.Metadata.Name || checkID(v.ID) != nil {
			return nil, fmt.Errorf("invalid volume state identity")
		}
		out = append(out, v)
	}
	return out, nil
}
func (a *API) Quarantine(ctx context.Context, node FenceNode) error {
	var current kubeNode
	path := "/api/v1/nodes/" + url.PathEscape(node.Name)
	if err := a.request(ctx, "GET", path, nil, &current); err != nil {
		return err
	}
	if !sameIncident(node, current.fenceNode()) {
		return fmt.Errorf("node incident changed before quarantine")
	}
	taints := current.Spec.Taints
	for _, t := range taints {
		if t["key"] == "node.kubernetes.io/out-of-service" && t["effect"] == "NoExecute" {
			return nil
		}
	}
	taints = append(taints, map[string]any{"key": "node.kubernetes.io/out-of-service", "value": "sqlite-csi-fenced", "effect": "NoExecute"})
	patch := []map[string]any{{"op": "test", "path": "/metadata/uid", "value": node.UID},
		{"op": "test", "path": "/metadata/resourceVersion", "value": current.Metadata.ResourceVersion},
		{"op": "add", "path": "/spec/unschedulable", "value": true}, {"op": "add", "path": "/spec/taints", "value": taints}}
	return a.request(ctx, "PATCH", path, patch, nil)
}

func prepareReplica(backing string) func(context.Context, Volume, RecoveryRecord) (RecoveredReplica, error) {
	return func(ctx context.Context, v Volume, record RecoveryRecord) (RecoveredReplica, error) {
		if checkID(v.ID) != nil || !validDatabase.MatchString(v.Database) || !validDatabase.MatchString(replicaDirectory(v)) || !strings.HasPrefix(record.ID, "r-") || !validDatabase.MatchString(record.ID) {
			return RecoveredReplica{}, fmt.Errorf("invalid recovery path identity")
		}
		relative := filepath.Join(v.ID, "recoveries", record.ID, "database.db")
		final := filepath.Join(backing, relative)
		if err := os.MkdirAll(filepath.Dir(final), 0770); err != nil {
			return RecoveredReplica{}, err
		}
		if !exists(final) {
			tmp, err := os.MkdirTemp(filepath.Dir(final), "restore-")
			if err != nil {
				return RecoveredReplica{}, err
			}
			defer os.RemoveAll(tmp)
			path := filepath.Join(tmp, "database.db")
			out, err := exec.CommandContext(ctx, "litestream", "restore", "-o", path, "file://"+filepath.Join(backing, v.ID, replicaDirectory(v))).CombinedOutput()
			if err != nil {
				return RecoveredReplica{}, fmt.Errorf("restore failed: %w: %s", err, out)
			}
			if _, err = exec.CommandContext(ctx, "python3", "-c", digestProgram, path).Output(); err != nil {
				return RecoveredReplica{}, fmt.Errorf("recovered database failed integrity check: %w", err)
			}
			f, err := os.Open(path)
			if err != nil {
				return RecoveredReplica{}, err
			}
			err = f.Sync()
			f.Close()
			if err != nil {
				return RecoveredReplica{}, err
			}
			if err = os.Rename(path, final); err != nil {
				return RecoveredReplica{}, err
			}
		}
		out, err := exec.CommandContext(ctx, "python3", "-c", digestProgram, final).Output()
		if err != nil {
			return RecoveredReplica{}, fmt.Errorf("recovery seed integrity check failed: %w", err)
		}
		return RecoveredReplica{Path: relative, Digest: strings.TrimSpace(string(out))}, nil
	}
}
