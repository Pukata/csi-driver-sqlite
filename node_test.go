package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type memoryStore struct{ volume Volume }

func (s *memoryStore) Get(context.Context, string) (Volume, error) { return s.volume, nil }
func (s *memoryStore) Create(_ context.Context, v Volume) (Volume, error) {
	s.volume = v
	return v, nil
}
func (s *memoryStore) Edit(_ context.Context, _ string, edit func(*Volume) error) (Volume, error) {
	v := s.volume
	if err := edit(&v); err != nil {
		return s.volume, err
	}
	s.volume = v
	return v, nil
}

func TestRecoveredSessionsRejectLegacyState(t *testing.T) {
	for _, tc := range []struct {
		name, volume, directory string
	}{
		{"policy", `{"recoveryPolicy":"latest-replica"}`, "replica"},
		{"record", `{"recovery":{"phase":"released","unknownField":true}}`, "replica"},
		{"empty-record", `{"recovery":{}}`, "replica"},
		{"seed", `{"recoverySeed":"old/seed"}`, "replica"},
		{"digest", `{"seedDigest":"old"}`, "replica"},
		{"claimed", `{"seedClaimedEpoch":7}`, "replica"},
		{"volume-directory", `{"replicaDirectory":"replica-old"}`, "replica"},
		{"session-directory", `{}`, "replica-old"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v := Volume{ID: "v-0123456789abcdef0123456789abcdef", Owner: "node-a", Epoch: 7, Prepared: true}
			if err := json.Unmarshal([]byte(tc.volume), &v); err != nil {
				t.Fatal(err)
			}
			store := &memoryStore{volume: v}
			n := &Node{store: store, id: v.Owner, root: t.TempDir(), kubelet: t.TempDir(), sessions: map[string]*Session{}, processes: map[string]*process{}}
			target := filepath.Join(n.kubelet, "pods/pod/volumes/kubernetes.io~csi/pvc/mount")
			s := &Session{ID: v.ID, Epoch: v.Epoch, Database: "database.db", Ready: true, Targets: map[string]bool{target: false}, ReplicaDirectory: tc.directory}
			if err := os.MkdirAll(n.base(v.ID), 0700); err != nil {
				t.Fatal(err)
			}
			if err := n.save(s); err != nil {
				t.Fatal(err)
			}
			if err := n.recoverSessions(); err != nil {
				t.Fatal(err)
			}
			s = n.sessions[v.ID]
			n.superviseSession(context.Background(), s)
			if exists(filepath.Join(n.base(v.ID), "litestream.json")) || len(n.processes) != 0 {
				t.Fatal("restart attempted legacy replication")
			}
			_, err := n.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
				VolumeId: v.ID, TargetPath: target, PublishContext: map[string]string{"epoch": "7"},
				VolumeCapability: &csi.VolumeCapability{AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}}, AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_SINGLE_WRITER}},
			})
			if status.Code(err) != codes.FailedPrecondition {
				t.Fatalf("legacy republish: %v", err)
			}
			_, err = n.NodeUnpublishVolume(context.Background(), &csi.NodeUnpublishVolumeRequest{VolumeId: v.ID, TargetPath: target})
			if status.Code(err) != codes.FailedPrecondition {
				t.Fatalf("legacy unpublish: %v", err)
			}
			if !reflect.DeepEqual(store.volume, v) {
				t.Fatal("legacy rejection changed persisted state")
			}
		})
	}
}

func TestCreateRejectsAutomaticRecovery(t *testing.T) {
	c := &Controller{store: &memoryStore{}, backing: t.TempDir()}
	_, err := c.CreateVolume(context.Background(), &csi.CreateVolumeRequest{
		Name: "automatic", Parameters: map[string]string{"recoveryPolicy": "latest-replica"},
		VolumeCapabilities: []*csi.VolumeCapability{{AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}}, AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_SINGLE_WRITER}}},
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("automatic policy: %v", err)
	}
}
