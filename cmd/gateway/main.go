package main

import (
	"flag"
	"fmt"
	"io"
	"log"
	"os"

	"github.com/owlfedoradev/kube-log-mcp/internal/kubernetes"
	"github.com/owlfedoradev/kube-log-mcp/internal/mcp"
	"github.com/owlfedoradev/kube-log-mcp/internal/prometheus"
)

func main() {
	installPath := flag.String("install", "", "Copy the gateway binary to this path and exit (useful for initContainers)")
	flag.Parse()

	if *installPath != "" {
		if err := installSelf(*installPath); err != nil {
			log.Fatalf("Failed to install: %v", err)
		}
		log.Printf("Successfully installed to %s", *installPath)
		return
	}

	// Initialize Kubernetes Client
	k8sClient, err := kubernetes.NewClient()
	if err != nil {
		log.Printf("Warning: Failed to initialize Kubernetes client (running out-of-cluster?): %v", err)
	} else {
		log.Printf("Successfully initialized Kubernetes client")
	}

	// Initialize Prometheus Client
	promClient, err := prometheus.NewClient()
	if err != nil {
		log.Printf("Warning: Failed to initialize Prometheus client: %v", err)
	} else {
		log.Printf("Successfully initialized Prometheus client")
	}

	// Initialize the MCP Server
	gateway := mcp.NewGatewayServer(k8sClient, promClient)

	// Serve over Standard I/O (JSON-RPC)
	// The agent runner will spawn this process and communicate via stdio
	if err := gateway.ServeStdio(); err != nil {
		log.Fatalf("MCP Server error: %v", err)
	}
}

func installSelf(targetPath string) error {
	exePath, err := os.Executable()
	if err != nil {
		return fmt.Errorf("could not get executable path: %v", err)
	}

	src, err := os.Open(exePath)
	if err != nil {
		return fmt.Errorf("could not open source binary: %v", err)
	}
	defer src.Close()

	dst, err := os.OpenFile(targetPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0755)
	if err != nil {
		return fmt.Errorf("could not open target file: %v", err)
	}
	defer dst.Close()

	if _, err := io.Copy(dst, src); err != nil {
		return fmt.Errorf("could not copy binary: %v", err)
	}

	return nil
}
