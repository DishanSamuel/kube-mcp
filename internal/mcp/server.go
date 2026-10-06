package mcp

import (
	"context"
	"fmt"

	"github.com/owlfedoradev/kube-log-mcp/internal/kubernetes"
	"github.com/owlfedoradev/kube-log-mcp/internal/prometheus"
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

type GatewayServer struct {
	mcpServer *server.MCPServer
	k8sClient *kubernetes.Client
	promClient *prometheus.Client
}

func NewGatewayServer(k8sClient *kubernetes.Client, promClient *prometheus.Client) *GatewayServer {
	s := server.NewMCPServer(
		"kube-log-mcp",
		"1.0.0",
		server.WithToolCapabilities(true),
	)

	gateway := &GatewayServer{
		mcpServer: s,
		k8sClient: k8sClient,
		promClient: promClient,
	}

	gateway.registerTools()

	return gateway
}

func (s *GatewayServer) registerTools() {
	// 1. get_deployment_status
	getDeploymentStatusTool := mcp.NewTool("get_deployment_status",
		mcp.WithDescription("Retrieves replica counts, readiness states, and recent events for a given deployment."),
		mcp.WithString("namespace", mcp.Required(), mcp.Description("The Kubernetes namespace")),
		mcp.WithString("deployment", mcp.Required(), mcp.Description("The name of the deployment")),
	)
	s.mcpServer.AddTool(getDeploymentStatusTool, s.handleGetDeploymentStatus)

	// 2. search_pod_logs
	searchPodLogsTool := mcp.NewTool("search_pod_logs",
		mcp.WithDescription("Streams and filters a capped number of tail lines from a specific pod, preventing massive payload dumps."),
		mcp.WithString("namespace", mcp.Required(), mcp.Description("The Kubernetes namespace")),
		mcp.WithString("pod_prefix", mcp.Required(), mcp.Description("The prefix of the pod name")),
		mcp.WithNumber("lines", mcp.Description("The number of tail lines (max 500)")),
	)
	s.mcpServer.AddTool(searchPodLogsTool, s.handleSearchPodLogs)

	// 3. get_process_metrics
	getProcessMetricsTool := mcp.NewTool("get_process_metrics",
		mcp.WithDescription("Translates simple requests (e.g., 'CPU for node_pool_A') into complex, safe PromQL aggregations."),
		mcp.WithString("target", mcp.Required(), mcp.Description("The target to get metrics for (e.g. node_pool_A, payment-service)")),
		mcp.WithString("metric_type", mcp.Required(), mcp.Description("The type of metric: cpu, memory")),
	)
	s.mcpServer.AddTool(getProcessMetricsTool, s.handleGetProcessMetrics)
}

func (s *GatewayServer) handleGetDeploymentStatus(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args, ok := request.Params.Arguments.(map[string]interface{})
	if !ok {
		return mcp.NewToolResultError("Invalid arguments format"), nil
	}
	namespace := args["namespace"].(string)
	deployment := args["deployment"].(string)

	if s.k8sClient == nil {
		return mcp.NewToolResultError("Kubernetes client not initialized"), nil
	}

	result, err := s.k8sClient.GetDeploymentStatus(ctx, namespace, deployment)
	if err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("Error getting deployment status: %v", err)), nil
	}

	return mcp.NewToolResultText(result), nil
}

func (s *GatewayServer) handleSearchPodLogs(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args, ok := request.Params.Arguments.(map[string]interface{})
	if !ok {
		return mcp.NewToolResultError("Invalid arguments format"), nil
	}
	namespace := args["namespace"].(string)
	podPrefix := args["pod_prefix"].(string)
	
	var lines int64 = 500
	if l, ok := args["lines"].(float64); ok {
		lines = int64(l)
	}

	if s.k8sClient == nil {
		return mcp.NewToolResultError("Kubernetes client not initialized"), nil
	}

	result, err := s.k8sClient.SearchPodLogs(ctx, namespace, podPrefix, lines)
	if err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("Error searching pod logs: %v", err)), nil
	}

	return mcp.NewToolResultText(result), nil
}

func (s *GatewayServer) handleGetProcessMetrics(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args, ok := request.Params.Arguments.(map[string]interface{})
	if !ok {
		return mcp.NewToolResultError("Invalid arguments format"), nil
	}
	target := args["target"].(string)
	metricType := args["metric_type"].(string)

	if s.promClient == nil {
		return mcp.NewToolResultError("Prometheus client not initialized"), nil
	}

	result, err := s.promClient.GetProcessMetrics(ctx, target, metricType)
	if err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("Error getting process metrics: %v", err)), nil
	}

	return mcp.NewToolResultText(result), nil
}

func (s *GatewayServer) ServeStdio() error {
	return server.ServeStdio(s.mcpServer)
}
