package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type Session struct {
	ID               string          `json:"id"`
	Epoch            int64           `json:"epoch"`
	Database         string          `json:"database"`
	Ready            bool            `json:"ready"`
	ReadOnly         bool            `json:"readOnly"`
	Targets          map[string]bool `json:"targets"`
	ReplicaDirectory string          `json:"replicaDirectory,omitempty"`
}
type process struct {
	cmd  *exec.Cmd
	done chan struct{}
	err  error
}
type Node struct {
	csi.UnimplementedNodeServer
	mu                         sync.Mutex
	store                      Store
	id, root, backing, kubelet string
	sessions                   map[string]*Session
	processes                  map[string]*process
}

func (n *Node) base(id string) string { return filepath.Join(n.root, id) }
func (n *Node) work(s *Session) string {
	return filepath.Join(n.base(s.ID), fmt.Sprintf("epoch-%d", s.Epoch))
}
func (n *Node) database(s *Session) string { return filepath.Join(n.work(s), s.Database) }
func (n *Node) replica(id, directory string) string {
	if directory == "" {
		directory = "replica"
	}
	return filepath.Join(n.backing, id, directory)
}
func ownDir(path string) error {
	if e := os.MkdirAll(path, 0770); e != nil {
		return e
	}
	return os.Chown(path, 1000, 1000)
}
func writeJSON(path string, v any) error {
	b, e := json.Marshal(v)
	if e != nil {
		return e
	}
	temp := path + ".tmp"
	f, e := os.OpenFile(temp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0644)
	if e != nil {
		return e
	}
	if _, e = f.Write(b); e == nil {
		e = f.Sync()
	}
	f.Close()
	if e != nil {
		return e
	}
	if e = os.Rename(temp, path); e != nil {
		return e
	}
	d, e := os.Open(filepath.Dir(path))
	if e == nil {
		e = d.Sync()
		d.Close()
	}
	return e
}
func (n *Node) save(s *Session) error {
	return writeJSON(filepath.Join(n.base(s.ID), "session.json"), s)
}
func command(ctx context.Context, name string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: 1000, Gid: 1000}}
	return cmd
}
func run(ctx context.Context, name string, args ...string) ([]byte, error) {
	output, e := command(ctx, name, args...).CombinedOutput()
	if e != nil {
		return nil, fmt.Errorf("%s failed: %w: %s", name, e, output)
	}
	return output, nil
}
func exists(path string) bool  { _, e := os.Stat(path); return e == nil }
func mounted(path string) bool { return exec.Command("mountpoint", "-q", path).Run() == nil }
func (n *Node) target(path string) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || !strings.HasPrefix(path, n.kubelet+"/pods/") || !strings.Contains(path, "/volumes/kubernetes.io~csi/") {
		return status.Error(codes.InvalidArgument, "target must be a CSI pod mount under the configured kubelet root")
	}
	return nil
}
func (n *Node) start(s *Session) error {
	if s.ReplicaDirectory != "" && !validDatabase.MatchString(s.ReplicaDirectory) {
		return fmt.Errorf("invalid replica directory")
	}
	if p := n.processes[s.ID]; p != nil {
		select {
		case <-p.done:
			delete(n.processes, s.ID)
		default:
			return nil
		}
	}
	path := filepath.Join(n.base(s.ID), "litestream.json")
	config := map[string]any{"dbs": []any{map[string]any{"path": n.database(s), "replica": map[string]any{"path": n.replica(s.ID, s.ReplicaDirectory)}}}}
	if e := writeJSON(path, config); e != nil {
		return e
	}
	cmd := command(context.Background(), "litestream", "replicate", "-config", path)
	logfile, e := os.OpenFile(filepath.Join(n.base(s.ID), "litestream.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if e != nil {
		return e
	}
	cmd.Stdout = logfile
	cmd.Stderr = logfile
	if e = cmd.Start(); e != nil {
		logfile.Close()
		return e
	}
	p := &process{cmd: cmd, done: make(chan struct{})}
	n.processes[s.ID] = p
	go func() { p.err = cmd.Wait(); logfile.Close(); close(p.done) }()
	return nil
}
func (n *Node) stop(id string) error {
	p := n.processes[id]
	if p == nil {
		return nil
	}
	select {
	case <-p.done:
		delete(n.processes, id)
		return p.err
	default:
	}
	if e := p.cmd.Process.Signal(syscall.SIGTERM); e != nil {
		return e
	}
	select {
	case <-p.done:
		delete(n.processes, id)
		return p.err
	case <-time.After(30 * time.Second):
		p.cmd.Process.Kill()
		<-p.done
		delete(n.processes, id)
		return fmt.Errorf("replica did not stop cleanly")
	}
}

const digestProgram = `import sqlite3,hashlib,json,sys
db=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True,timeout=15)
db.execute('begin')
assert db.execute('pragma integrity_check').fetchone()[0]=='ok', 'SQLite integrity failure'
digest=hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()
db.rollback();db.close()
print(digest)
`

func digest(ctx context.Context, path string) (string, error) {
	out, e := run(ctx, "python3", "-c", digestProgram, path)
	return strings.TrimSpace(string(out)), e
}
func (n *Node) restore(ctx context.Context, id, directory, path string) error {
	if directory != "" && !validDatabase.MatchString(directory) {
		return fmt.Errorf("invalid replica directory")
	}
	tmp, e := os.MkdirTemp(filepath.Dir(path), ".restore-")
	if e != nil {
		return e
	}
	defer os.RemoveAll(tmp)
	if e = os.Chown(tmp, 1000, 1000); e != nil {
		return e
	}
	staged := filepath.Join(tmp, "database.db")
	if _, e = run(ctx, "litestream", "restore", "-o", staged, "file://"+n.replica(id, directory)); e != nil {
		return e
	}
	if _, e = digest(ctx, staged); e != nil {
		return e
	}
	return os.Rename(staged, path)
}
func (n *Node) importSeed(ctx context.Context, v Volume, path string) error {
	if !strings.HasPrefix(v.RecoverySeed, v.ID+"/recoveries/") || filepath.Clean(v.RecoverySeed) != v.RecoverySeed {
		return fmt.Errorf("invalid recovery seed identity")
	}
	source := filepath.Join(n.backing, v.RecoverySeed)
	wanted, e := digest(ctx, source)
	if e != nil {
		return e
	}
	if wanted != v.SeedDigest {
		return fmt.Errorf("recovery seed contents changed")
	}
	tmp, e := os.MkdirTemp(filepath.Dir(path), ".seed-")
	if e != nil {
		return e
	}
	defer os.RemoveAll(tmp)
	if e = os.Chown(tmp, 1000, 1000); e != nil {
		return e
	}
	staged := filepath.Join(tmp, "database.db")
	program := "import os,shutil,sys; shutil.copyfile(sys.argv[1],sys.argv[2]); f=open(sys.argv[2],'rb'); os.fsync(f.fileno()); f.close()"
	if _, e = run(ctx, "python3", "-c", program, source, staged); e != nil {
		return e
	}
	got, e := digest(ctx, staged)
	if e != nil {
		return e
	}
	if got != wanted {
		return fmt.Errorf("local recovery import differs from verified seed")
	}
	return os.Rename(staged, path)
}
func (n *Node) flush(ctx context.Context, s *Session) (bool, error) {
	if !exists(n.database(s)) {
		v, e := n.store.Get(ctx, s.ID)
		if e != nil {
			return false, e
		}
		if v.Initialized {
			return false, fmt.Errorf("initialized local database disappeared")
		}
		return false, nil
	}
	if !s.ReadOnly {
		if e := n.start(s); e != nil {
			return false, e
		}
		select {
		case <-time.After(2 * time.Second):
		case <-ctx.Done():
			return false, ctx.Err()
		}
		if e := n.stop(s.ID); e != nil {
			return false, e
		}
	}
	wanted, e := digest(ctx, n.database(s))
	if e != nil {
		return false, e
	}
	if s.ReadOnly {
		v, err := n.store.Get(ctx, s.ID)
		if err != nil {
			return false, err
		}
		if v.RecoverySeed != "" && v.SeedClaimedEpoch == s.Epoch {
			source, err := digest(ctx, filepath.Join(n.backing, v.RecoverySeed))
			if err != nil {
				return false, err
			}
			if source != v.SeedDigest || source != wanted {
				return false, fmt.Errorf("read-only recovery seed differs from local database")
			}
			return true, nil
		}
	}
	scratch, e := os.MkdirTemp(n.base(s.ID), "check-")
	if e != nil {
		return false, e
	}
	defer os.RemoveAll(scratch)
	if e = os.Chown(scratch, 1000, 1000); e != nil {
		return false, e
	}
	path := filepath.Join(scratch, "check.db")
	if e = n.restore(ctx, s.ID, s.ReplicaDirectory, path); e != nil {
		return false, e
	}
	got, e := digest(ctx, path)
	if e != nil {
		return false, e
	}
	if got != wanted {
		return false, fmt.Errorf("final SQLite contents differ from shared replica; ownership remains locked")
	}
	return true, nil
}
func (n *Node) NodePublishVolume(ctx context.Context, r *csi.NodePublishVolumeRequest) (*csi.NodePublishVolumeResponse, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if e := checkID(r.VolumeId); e != nil {
		return nil, e
	}
	if e := checkCapability(r.VolumeCapability); e != nil {
		return nil, e
	}
	if e := n.target(r.TargetPath); e != nil {
		return nil, e
	}
	epoch, e := strconv.ParseInt(r.PublishContext["epoch"], 10, 64)
	if e != nil {
		return nil, status.Error(codes.InvalidArgument, "attachment epoch missing")
	}
	v, e := n.store.Edit(ctx, r.VolumeId, func(v *Volume) error {
		if v.Owner != n.id || v.Epoch != epoch || v.Deleted {
			return status.Error(codes.FailedPrecondition, "volume is not attached to this node and epoch")
		}
		v.Prepared = true
		v.Clean = false
		if v.RecoverySeed != "" && v.SeedClaimedEpoch == 0 {
			v.SeedClaimedEpoch = epoch
		}
		return nil
	})
	if e != nil {
		return nil, e
	}
	s := n.sessions[r.VolumeId]
	if s != nil && s.Epoch != epoch && len(s.Targets) > 0 {
		return nil, status.Error(codes.FailedPrecondition, "previous local session still has a target")
	}
	if s == nil || s.Epoch != epoch {
		if mounted(r.TargetPath) {
			return nil, status.Error(codes.FailedPrecondition, "unknown existing mount; refusing adoption")
		}
		if e = ownDir(n.base(v.ID)); e != nil {
			return nil, e
		}
		s = &Session{ID: v.ID, Epoch: epoch, Database: v.Database, ReadOnly: r.Readonly, Targets: map[string]bool{}, ReplicaDirectory: replicaDirectory(v)}
		n.sessions[v.ID] = s
		if e = n.save(s); e != nil {
			return nil, e
		}
	}
	if _, ok := s.Targets[r.TargetPath]; ok {
		if s.ReadOnly != r.Readonly {
			return nil, status.Error(codes.AlreadyExists, "mount flags differ")
		}
		if mounted(r.TargetPath) {
			if !s.ReadOnly {
				e = n.start(s)
			}
			return &csi.NodePublishVolumeResponse{}, e
		}
	} else if len(s.Targets) > 0 {
		return nil, status.Error(codes.FailedPrecondition, "ReadWriteOncePod: another pod target is recorded")
	}
	if len(s.Targets) == 0 {
		s.ReadOnly = r.Readonly
	}
	if !s.Ready {
		if e = ownDir(n.work(s)); e != nil {
			return nil, e
		}
		if v.Initialized && !exists(n.database(s)) {
			if v.RecoverySeed != "" && v.SeedClaimedEpoch == epoch {
				e = n.importSeed(ctx, v, n.database(s))
			} else {
				e = n.restore(ctx, v.ID, s.ReplicaDirectory, n.database(s))
			}
			if e != nil {
				return nil, e
			}
		}
		if exists(n.database(s)) {
			if _, e = digest(ctx, n.database(s)); e != nil {
				return nil, e
			}
		}
		if r.Readonly && !exists(n.database(s)) {
			return nil, status.Error(codes.FailedPrecondition, "cannot read an uninitialized SQLite volume")
		}
		s.Ready = true
		if e = n.save(s); e != nil {
			return nil, e
		}
	}
	if !s.ReadOnly {
		if e = n.start(s); e != nil {
			return nil, e
		}
	}
	if e = os.MkdirAll(r.TargetPath, 0750); e != nil {
		return nil, e
	}
	// Persist intent before exposing a writable mount, so restart cannot lose refs.
	s.Targets[r.TargetPath] = r.Readonly
	if e = n.save(s); e != nil {
		return nil, e
	}
	if !mounted(r.TargetPath) {
		if output, err := exec.CommandContext(ctx, "mount", "--bind", n.work(s), r.TargetPath).CombinedOutput(); err != nil {
			return nil, fmt.Errorf("bind mount failed: %w: %s", err, output)
		}
	}
	if r.Readonly {
		if output, err := exec.CommandContext(ctx, "mount", "-o", "remount,bind,ro", r.TargetPath).CombinedOutput(); err != nil {
			return nil, fmt.Errorf("read-only remount failed: %w: %s", err, output)
		}
	}
	return &csi.NodePublishVolumeResponse{}, nil
}
func (n *Node) NodeUnpublishVolume(ctx context.Context, r *csi.NodeUnpublishVolumeRequest) (*csi.NodeUnpublishVolumeResponse, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if e := checkID(r.VolumeId); e != nil {
		return nil, e
	}
	if e := n.target(r.TargetPath); e != nil {
		return nil, e
	}
	s := n.sessions[r.VolumeId]
	if s == nil {
		if mounted(r.TargetPath) {
			return nil, status.Error(codes.FailedPrecondition, "unknown mount; refusing ownership release")
		}
		return &csi.NodeUnpublishVolumeResponse{}, nil
	}
	v, e := n.store.Get(ctx, s.ID)
	if e != nil {
		return nil, e
	}
	stale := v.Owner != n.id || v.Epoch != s.Epoch
	if stale {
		if e = n.stop(s.ID); e != nil {
			log.Printf("stale publisher exit %s: %v", s.ID, e)
		}
	}
	if mounted(r.TargetPath) {
		if out, e := exec.CommandContext(ctx, "umount", r.TargetPath).CombinedOutput(); e != nil {
			return nil, fmt.Errorf("unmount failed: %w: %s", e, out)
		}
	}
	delete(s.Targets, r.TargetPath)
	if e := n.save(s); e != nil {
		return nil, e
	}
	if stale {
		return &csi.NodeUnpublishVolumeResponse{}, nil
	}
	if len(s.Targets) > 0 {
		return &csi.NodeUnpublishVolumeResponse{}, nil
	}
	initialized, e := n.flush(ctx, s)
	if e != nil {
		return nil, e
	}
	_, e = n.store.Edit(ctx, s.ID, func(v *Volume) error {
		if v.Owner != n.id || v.Epoch != s.Epoch {
			return status.Error(codes.FailedPrecondition, "attachment changed before clean release")
		}
		v.Clean = true
		v.Initialized = v.Initialized || initialized
		if initialized && !s.ReadOnly {
			v.RecoverySeed = ""
			v.SeedDigest = ""
			v.SeedClaimedEpoch = 0
			if v.Recovery != nil {
				v.Recovery.Phase = "complete"
			}
		} else if s.ReadOnly && v.RecoverySeed != "" {
			v.SeedClaimedEpoch = 0
		}
		return nil
	})
	if e != nil {
		return nil, e
	}
	return &csi.NodeUnpublishVolumeResponse{}, nil
}
func (n *Node) NodeGetInfo(context.Context, *csi.NodeGetInfoRequest) (*csi.NodeGetInfoResponse, error) {
	return &csi.NodeGetInfoResponse{NodeId: n.id}, nil
}
func (n *Node) NodeGetCapabilities(context.Context, *csi.NodeGetCapabilitiesRequest) (*csi.NodeGetCapabilitiesResponse, error) {
	return &csi.NodeGetCapabilitiesResponse{Capabilities: []*csi.NodeServiceCapability{{Type: &csi.NodeServiceCapability_Rpc{Rpc: &csi.NodeServiceCapability_RPC{Type: csi.NodeServiceCapability_RPC_SINGLE_NODE_MULTI_WRITER}}}}}, nil
}
func (n *Node) recoverSessions() error {
	entries, e := os.ReadDir(n.root)
	if e != nil {
		return e
	}
	for _, entry := range entries {
		if !entry.IsDir() || !validID.MatchString(entry.Name()) {
			continue
		}
		b, e := os.ReadFile(filepath.Join(n.root, entry.Name(), "session.json"))
		if os.IsNotExist(e) {
			continue
		}
		if e != nil {
			return e
		}
		var s Session
		if e = json.Unmarshal(b, &s); e != nil {
			return e
		}
		if s.ID != entry.Name() {
			return fmt.Errorf("session ID mismatch")
		}
		if s.Targets == nil {
			s.Targets = map[string]bool{}
		}
		n.sessions[s.ID] = &s
	}
	return nil
}
func (n *Node) supervise(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			n.mu.Lock()
			for id := range n.processes {
				if e := n.stop(id); e != nil {
					log.Printf("replica shutdown %s: %v", id, e)
				}
			}
			n.mu.Unlock()
			return
		case <-ticker.C:
			n.mu.Lock()
			for _, s := range n.sessions {
				if len(s.Targets) == 0 || s.ReadOnly || !s.Ready {
					continue
				}
				v, e := n.store.Get(ctx, s.ID)
				if e != nil {
					continue
				}
				if v.Owner != n.id || v.Epoch != s.Epoch {
					log.Printf("ownership mismatch for %s; refusing replication", s.ID)
					n.stop(s.ID)
					continue
				}
				if e = n.start(s); e != nil {
					log.Printf("replica restart %s: %v", s.ID, e)
				}
			}
			n.mu.Unlock()
		}
	}
}
