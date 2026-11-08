package main

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const driverName = "sqlite.csi"

var validID = regexp.MustCompile(`^v-[a-f0-9]{32}$`)
var validDatabase = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._-]*$`)

type Controller struct {
	csi.UnimplementedControllerServer
	store   Store
	backing string
}

func checkCapability(cap *csi.VolumeCapability) error {
	if cap == nil || cap.GetMount() == nil || cap.GetAccessMode().GetMode() != csi.VolumeCapability_AccessMode_SINGLE_NODE_SINGLE_WRITER {
		return status.Error(codes.InvalidArgument, "only filesystem ReadWriteOncePod volumes are supported")
	}
	return nil
}
func checkID(id string) error {
	if !validID.MatchString(id) {
		return status.Error(codes.InvalidArgument, "invalid volume ID")
	}
	return nil
}
func acquire(v *Volume, node string) error {
	if e := checkLegacyRecovery(*v); e != nil {
		return e
	}
	if v.Deleted {
		return status.Error(codes.NotFound, "volume is retained but deleted")
	}
	if v.Owner != "" && v.Owner != node {
		return status.Error(codes.FailedPrecondition, "previous node still owns this volume; no timeout-based takeover")
	}
	if v.Owner == "" {
		v.Owner = node
		v.Epoch++
		v.Prepared = false
		v.Clean = false
	}
	return nil
}
func release(v *Volume, node string) error {
	if e := checkLegacyRecovery(*v); e != nil {
		return e
	}
	if v.Owner == "" {
		return nil
	}
	if v.Owner != node {
		return status.Error(codes.FailedPrecondition, "different node owns volume")
	}
	if v.Prepared && !v.Clean {
		return status.Error(codes.FailedPrecondition, "node has not verified a clean final replica; restore NAS access and retry clean unmount")
	}
	v.Owner = ""
	return nil
}
func (c *Controller) CreateVolume(ctx context.Context, r *csi.CreateVolumeRequest) (*csi.CreateVolumeResponse, error) {
	if r.Name == "" || len(r.VolumeCapabilities) == 0 {
		return nil, status.Error(codes.InvalidArgument, "name and capabilities required")
	}
	if r.VolumeContentSource != nil {
		return nil, status.Error(codes.InvalidArgument, "cloning and snapshot sources unsupported")
	}
	for _, cap := range r.VolumeCapabilities {
		if e := checkCapability(cap); e != nil {
			return nil, e
		}
	}
	database := r.Parameters["database"]
	policy := r.Parameters["recoveryPolicy"]
	if policy != "" && policy != "manual" {
		return nil, status.Error(codes.InvalidArgument, "unsupported recoveryPolicy")
	}
	if database == "" {
		database = "database.db"
	}
	if !validDatabase.MatchString(database) || database == "." || database == ".." || len(database) > 100 {
		return nil, status.Error(codes.InvalidArgument, "database must be a single safe filename")
	}
	sum := sha256.Sum256([]byte(r.Name))
	id := fmt.Sprintf("v-%x", sum[:16])
	capacity := r.GetCapacityRange().GetRequiredBytes()
	if capacity < 1 {
		capacity = 1 << 30
	}
	v, e := c.store.Create(ctx, Volume{ID: id, Database: database, Capacity: capacity, RecoveryPolicy: policy})
	if e != nil {
		return nil, e
	}
	if v.Deleted || v.Database != database || v.Capacity < capacity || normalizedPolicy(v.RecoveryPolicy) != normalizedPolicy(policy) {
		return nil, status.Error(codes.AlreadyExists, "volume exists with different parameters")
	}
	if e = checkLegacyRecovery(v); e != nil {
		return nil, e
	}
	if e = os.MkdirAll(filepath.Join(c.backing, id, "replica"), 0770); e != nil {
		return nil, e
	}
	return &csi.CreateVolumeResponse{Volume: &csi.Volume{VolumeId: id, CapacityBytes: v.Capacity, VolumeContext: map[string]string{"database": database}}}, nil
}
func (c *Controller) DeleteVolume(ctx context.Context, r *csi.DeleteVolumeRequest) (*csi.DeleteVolumeResponse, error) {
	if e := checkID(r.VolumeId); e != nil {
		return nil, e
	}
	_, e := c.store.Edit(ctx, r.VolumeId, func(v *Volume) error {
		if v.Owner != "" {
			return status.Error(codes.FailedPrecondition, "volume is owned")
		}
		v.Deleted = true
		return nil
	})
	if apiCode(e, 404) {
		e = nil
	}
	return &csi.DeleteVolumeResponse{}, e // Data is deliberately retained.
}
func (c *Controller) ControllerPublishVolume(ctx context.Context, r *csi.ControllerPublishVolumeRequest) (*csi.ControllerPublishVolumeResponse, error) {
	if e := checkID(r.VolumeId); e != nil {
		return nil, e
	}
	if r.NodeId == "" {
		return nil, status.Error(codes.InvalidArgument, "node required")
	}
	if e := checkCapability(r.VolumeCapability); e != nil {
		return nil, e
	}
	v, e := c.store.Edit(ctx, r.VolumeId, func(v *Volume) error { return acquire(v, r.NodeId) })
	if e != nil {
		return nil, e
	}
	return &csi.ControllerPublishVolumeResponse{PublishContext: map[string]string{"epoch": strconv.FormatInt(v.Epoch, 10)}}, nil
}
func (c *Controller) ControllerUnpublishVolume(ctx context.Context, r *csi.ControllerUnpublishVolumeRequest) (*csi.ControllerUnpublishVolumeResponse, error) {
	if e := checkID(r.VolumeId); e != nil {
		return nil, e
	}
	if r.NodeId == "" {
		return nil, status.Error(codes.InvalidArgument, "explicit node required")
	}
	_, e := c.store.Edit(ctx, r.VolumeId, func(v *Volume) error { return release(v, r.NodeId) })
	if apiCode(e, 404) {
		e = nil
	}
	return &csi.ControllerUnpublishVolumeResponse{}, e
}
func (c *Controller) ValidateVolumeCapabilities(ctx context.Context, r *csi.ValidateVolumeCapabilitiesRequest) (*csi.ValidateVolumeCapabilitiesResponse, error) {
	if e := checkID(r.VolumeId); e != nil {
		return nil, e
	}
	if _, e := c.store.Get(ctx, r.VolumeId); e != nil {
		return nil, e
	}
	for _, cap := range r.VolumeCapabilities {
		if e := checkCapability(cap); e != nil {
			return &csi.ValidateVolumeCapabilitiesResponse{Message: e.Error()}, nil
		}
	}
	return &csi.ValidateVolumeCapabilitiesResponse{Confirmed: &csi.ValidateVolumeCapabilitiesResponse_Confirmed{VolumeCapabilities: r.VolumeCapabilities, VolumeContext: r.VolumeContext, Parameters: r.Parameters}}, nil
}
func (c *Controller) ControllerGetCapabilities(context.Context, *csi.ControllerGetCapabilitiesRequest) (*csi.ControllerGetCapabilitiesResponse, error) {
	out := &csi.ControllerGetCapabilitiesResponse{}
	for _, kind := range []csi.ControllerServiceCapability_RPC_Type{csi.ControllerServiceCapability_RPC_CREATE_DELETE_VOLUME, csi.ControllerServiceCapability_RPC_PUBLISH_UNPUBLISH_VOLUME, csi.ControllerServiceCapability_RPC_SINGLE_NODE_MULTI_WRITER} {
		out.Capabilities = append(out.Capabilities, &csi.ControllerServiceCapability{Type: &csi.ControllerServiceCapability_Rpc{Rpc: &csi.ControllerServiceCapability_RPC{Type: kind}}})
	}
	return out, nil
}
