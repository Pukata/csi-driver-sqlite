package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/wrapperspb"
)

type Identity struct {
	csi.UnimplementedIdentityServer
}

var version = "0.2.0-dev"

func (*Identity) GetPluginInfo(context.Context, *csi.GetPluginInfoRequest) (*csi.GetPluginInfoResponse, error) {
	return &csi.GetPluginInfoResponse{Name: driverName, VendorVersion: version}, nil
}
func (*Identity) GetPluginCapabilities(context.Context, *csi.GetPluginCapabilitiesRequest) (*csi.GetPluginCapabilitiesResponse, error) {
	return &csi.GetPluginCapabilitiesResponse{Capabilities: []*csi.PluginCapability{{Type: &csi.PluginCapability_Service_{Service: &csi.PluginCapability_Service{Type: csi.PluginCapability_Service_CONTROLLER_SERVICE}}}}}, nil
}
func (*Identity) Probe(context.Context, *csi.ProbeRequest) (*csi.ProbeResponse, error) {
	return &csi.ProbeResponse{Ready: wrapperspb.Bool(true)}, nil
}
func main() {
	mode := flag.String("mode", "node", "node or controller")
	endpoint := flag.String("endpoint", "/csi/csi.sock", "Unix socket")
	root := flag.String("local-root", "/var/lib/sqlite-csi", "node SSD directory")
	backing := flag.String("backing-root", "/replicas", "shared replica mount")
	kubelet := flag.String("kubelet-root", "/var/lib/kubelet", "kubelet root")
	namespace := flag.String("namespace", "sqlite-csi-system", "state namespace")
	nodeID := flag.String("node-id", os.Getenv("NODE_NAME"), "Kubernetes node name")
	fencingConfig := flag.String("fencing-config", "", "opt-in external fencing configuration")
	flag.Parse()
	if *mode != "node" && *mode != "controller" {
		log.Fatal("mode must be node or controller")
	}
	api, e := newAPI(*namespace)
	if e != nil {
		log.Fatal(e)
	}
	if e = os.MkdirAll(filepath.Dir(*endpoint), 0755); e != nil {
		log.Fatal(e)
	}
	os.Remove(*endpoint)
	listener, e := net.Listen("unix", *endpoint)
	if e != nil {
		log.Fatal(e)
	}
	server := grpc.NewServer(grpc.UnaryInterceptor(func(ctx context.Context, req any, info *grpc.UnaryServerInfo, next grpc.UnaryHandler) (any, error) {
		resp, e := next(ctx, req)
		if e != nil {
			log.Printf("%s: %v", info.FullMethod, e)
		}
		return resp, e
	}))
	csi.RegisterIdentityServer(server, &Identity{})
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	var supervisorDone chan struct{}
	if *mode == "controller" {
		csi.RegisterControllerServer(server, &Controller{store: api, backing: *backing, recoveryEnabled: *fencingConfig != ""})
		if *fencingConfig != "" {
			fencer, err := loadFencer(*fencingConfig)
			if err != nil {
				log.Fatal(err)
			}
			recovery := &RecoveryController{Store: api, Cluster: api, Fencer: fencer, Grace: time.Duration(fencer.config.GraceSeconds) * time.Second, Now: time.Now, Prepare: prepareReplica(*backing)}
			supervisorDone = make(chan struct{})
			go func() { defer close(supervisorDone); recovery.run(ctx) }()
		}
	} else {
		if *nodeID == "" {
			log.Fatal("node ID required")
		}
		if e = os.MkdirAll(*root, 0750); e != nil {
			log.Fatal(e)
		}
		n := &Node{store: api, id: *nodeID, root: *root, backing: *backing, kubelet: *kubelet, sessions: map[string]*Session{}, processes: map[string]*process{}}
		if e = n.recoverSessions(); e != nil {
			log.Fatal(e)
		}
		csi.RegisterNodeServer(server, n)
		supervisorDone = make(chan struct{})
		go func() { defer close(supervisorDone); n.supervise(ctx) }()
	}
	go func() { <-ctx.Done(); server.GracefulStop() }()
	fmt.Printf("%s %s mode=%s\n", driverName, version, *mode)
	if e = server.Serve(listener); e != nil {
		log.Fatal(e)
	}
	if supervisorDone != nil {
		<-supervisorDone
	}
}
