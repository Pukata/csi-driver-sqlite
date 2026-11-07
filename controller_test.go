package main

import (
	"context"
	"testing"

	"github.com/container-storage-interface/spec/lib/go/csi"
)

func TestNoTimeoutTakeover(t *testing.T) {
	v := Volume{ID: "test"}
	if e := acquire(&v, "s1"); e != nil {
		t.Fatal(e)
	}
	v.Prepared = true
	if e := acquire(&v, "s2"); e == nil {
		t.Fatal("stole ownership")
	}
	if e := release(&v, "s1"); e == nil {
		t.Fatal("released unverified data")
	}
	v.Clean = true
	if e := release(&v, "s1"); e != nil {
		t.Fatal(e)
	}
	if e := acquire(&v, "s2"); e != nil {
		t.Fatal(e)
	}
	if v.Epoch != 2 || v.Clean || v.Prepared {
		t.Fatal(v)
	}
}
func TestIdempotentAttachAndCancelledUnusedAttachment(t *testing.T) {
	v := Volume{}
	acquire(&v, "s1")
	acquire(&v, "s1")
	if v.Epoch != 1 {
		t.Fatal(v)
	}
	if e := release(&v, "s2"); e == nil {
		t.Fatal("wrong owner detached")
	}
	if e := release(&v, "s1"); e != nil {
		t.Fatal(e)
	}
}
func TestRejectUnsafeCapabilities(t *testing.T) {
	if checkCapability(nil) == nil {
		t.Fatal("accepted nil")
	}
	for _, mode := range []csi.VolumeCapability_AccessMode_Mode{csi.VolumeCapability_AccessMode_MULTI_NODE_MULTI_WRITER, csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER} {
		if checkCapability(&csi.VolumeCapability{AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}}, AccessMode: &csi.VolumeCapability_AccessMode{Mode: mode}}) == nil {
			t.Fatal(mode)
		}
	}
}
func TestSafeMountTargets(t *testing.T) {
	n := &Node{kubelet: "/var/lib/kubelet"}
	for _, path := range []string{"/etc", "/var/lib/kubelet/pods/../etc", "/var/lib/kubelet/pods/uid/random"} {
		if n.target(path) == nil {
			t.Fatal(path)
		}
	}
	if e := n.target("/var/lib/kubelet/pods/uid/volumes/kubernetes.io~csi/pvc/mount"); e != nil {
		t.Fatal(e)
	}
}

var _ = context.Background
