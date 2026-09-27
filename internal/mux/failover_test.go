package mux

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"sync"
	"testing"

	"github.com/b-nnett/codex-subscription-router/internal/backend"
	"github.com/b-nnett/codex-subscription-router/internal/protocol"
	"github.com/b-nnett/codex-subscription-router/internal/state"
)

type fakeChild struct {
	id        string
	mu        sync.Mutex
	requests  []string
	sends     []protocol.Message
	turnError error
	onTurn    func()
}

func (f *fakeChild) AccountID() string { return f.id }
func (f *fakeChild) Close() error      { return nil }

func (f *fakeChild) Send(message protocol.Message) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.sends = append(f.sends, message)
	return nil
}

func (f *fakeChild) Request(_ context.Context, method string, params json.RawMessage) (protocol.Message, error) {
	f.mu.Lock()
	f.requests = append(f.requests, method)
	onTurn := f.onTurn
	turnError := f.turnError
	f.mu.Unlock()
	switch method {
	case "account/read":
		return protocol.Message{Result: json.RawMessage(`{"account":{"type":"chatgpt","email":"test@example.com","planType":"plus"}}`)}, nil
	case "account/rateLimits/read":
		return protocol.Message{Result: json.RawMessage(`{"rateLimits":{"primary":{"usedPercent":10,"windowDurationMins":300},"secondary":{"usedPercent":20,"windowDurationMins":10080}}}`)}, nil
	case "thread/read":
		return protocol.Message{Result: json.RawMessage(`{"thread":{"id":"thread-1","path":"/tmp/thread-1.jsonl","cwd":"/tmp","modelProvider":"openai"}}`)}, nil
	case "thread/resume":
		return protocol.Message{Result: json.RawMessage(`{"thread":{"id":"thread-1"}}`)}, nil
	case "turn/start":
		if onTurn != nil {
			onTurn()
		}
		if turnError != nil {
			return protocol.Message{}, turnError
		}
		return protocol.Message{Result: json.RawMessage(`{"thread":{"id":"thread-1"}}`)}, nil
	default:
		return protocol.Message{Result: json.RawMessage(`{}`)}, nil
	}
}

func failoverFixture(t *testing.T) (*Multiplexer, *state.Store, []state.Account, *bytes.Buffer) {
	t.Helper()
	root := t.TempDir()
	store, err := state.Open(root+"/mux", root+"/primary")
	if err != nil {
		t.Fatal(err)
	}
	for _, label := range []string{"Fallback 1", "Fallback 2"} {
		if _, err := store.AddAccount(label); err != nil {
			t.Fatal(err)
		}
	}
	output := &bytes.Buffer{}
	multiplexer, err := New(Options{RealExecutable: "/unused", Store: store, Output: output})
	if err != nil {
		t.Fatal(err)
	}
	accounts := store.Accounts()
	for _, account := range accounts {
		multiplexer.children[account.ID] = &fakeChild{id: account.ID}
		multiplexer.resetPreviews[account.ID] = ResetCreditsPreview{
			AccountID: account.ID, AvailableCount: 0,
		}
	}
	return multiplexer, store, accounts, output
}

func TestAsyncContinuationRetriesRejectedFallbackBeforePersistingOwnership(t *testing.T) {
	multiplexer, store, accounts, _ := failoverFixture(t)
	if err := store.SetThreadOwner("thread-1", accounts[0].ID); err != nil {
		t.Fatal(err)
	}
	first := multiplexer.children[accounts[1].ID].(*fakeChild)
	second := multiplexer.children[accounts[2].ID].(*fakeChild)
	first.turnError = errors.New("usage limit was stale")
	for _, child := range []*fakeChild{first, second} {
		child.onTurn = func() {
			owner, _ := store.ThreadOwner("thread-1")
			if owner != accounts[0].ID {
				t.Errorf("ownership changed before fallback acceptance: %s", owner)
			}
		}
	}
	active := activeTurn{
		accountID: accounts[0].ID,
		message: protocol.Message{
			Method: "turn/start",
			Params: json.RawMessage(`{"threadId":"thread-1","model":"gpt-test","input":[]}`),
		},
	}
	multiplexer.continueAfterAsyncUsageLimit("thread-1", active, accounts[0].ID)
	owner, _ := store.ThreadOwner("thread-1")
	if owner != accounts[2].ID {
		t.Fatalf("accepted second fallback was not persisted: %s", owner)
	}
	if countMethod(first.requests, "turn/start") != 1 || countMethod(second.requests, "turn/start") != 1 {
		t.Fatalf("fallback attempts were not exhaustive: first=%v second=%v", first.requests, second.requests)
	}
}

func TestSynchronousFailoverPersistsOwnershipOnlyAfterSuccessResponse(t *testing.T) {
	multiplexer, store, accounts, _ := failoverFixture(t)
	if err := store.SetThreadOwner("thread-1", accounts[0].ID); err != nil {
		t.Fatal(err)
	}
	message := protocol.Message{
		ID:     protocol.StringID("client-1"),
		Method: "turn/start",
		Params: json.RawMessage(`{"threadId":"thread-1","model":"gpt-test"}`),
	}
	multiplexer.failoverTurn(
		context.Background(),
		message,
		"thread-1",
		accounts[0].ID,
		map[string]struct{}{accounts[0].ID: {}},
	)
	owner, _ := store.ThreadOwner("thread-1")
	if owner != accounts[0].ID {
		t.Fatalf("ownership changed before response acceptance: %s", owner)
	}
	key := protocol.RequestIDKey(message.ID)
	multiplexer.externalMu.Lock()
	route, ok := multiplexer.externalRoutes[key]
	multiplexer.externalMu.Unlock()
	if !ok {
		t.Fatal("fallback route was not registered")
	}
	result := protocol.Message{ID: message.ID, Result: json.RawMessage(`{"thread":{"id":"thread-1"}}`)}
	if got := threadIDFromResult(result.Result); got != "thread-1" {
		t.Fatalf("test response did not contain thread ID: %q", got)
	}
	raw, err := protocol.Encode(result)
	if err != nil {
		t.Fatal(err)
	}
	multiplexer.handleInbound(backend.Inbound{AccountID: route.accountID, Message: result, Raw: raw})
	owner, _ = store.ThreadOwner("thread-1")
	if owner != route.accountID {
		t.Fatalf("accepted fallback owner was not persisted: %s (route=%s)", owner, route.accountID)
	}
}

func TestExternalProviderTurnNeverEntersQuotaFailoverTracking(t *testing.T) {
	multiplexer, store, accounts, output := failoverFixture(t)
	controller := accounts[0]
	config := []byte("model_provider = \"openrouter\"\n")
	if err := os.WriteFile(controller.CodexHome+"/config.toml", config, 0o600); err != nil {
		t.Fatal(err)
	}
	message := protocol.Message{
		ID:     protocol.StringID("external-1"),
		Method: "turn/start",
		Params: json.RawMessage(`{"threadId":"thread-external","modelProvider":"openrouter","model":"anthropic/claude"}`),
	}
	key := protocol.RequestIDKey(message.ID)
	multiplexer.externalRoutes[key] = externalRoute{
		accountID: controller.ID, method: message.Method, message: message,
	}
	result := protocol.Message{ID: message.ID, Result: json.RawMessage(`{"thread":{"id":"thread-external"}}`)}
	raw, _ := protocol.Encode(result)
	multiplexer.handleInbound(backend.Inbound{AccountID: controller.ID, Message: result, Raw: raw})
	if len(multiplexer.activeTurns) != 0 {
		t.Fatalf("external-provider turn was tracked for ChatGPT failover: %#v", multiplexer.activeTurns)
	}
	before := output.Len()
	notification := protocol.Message{
		Method: "error",
		Params: json.RawMessage(`{"threadId":"thread-external","willRetry":false,"error":{"message":"usage limit"}}`),
	}
	notificationRaw, _ := protocol.Encode(notification)
	multiplexer.handleInbound(backend.Inbound{AccountID: controller.ID, Message: notification, Raw: notificationRaw})
	if output.Len() <= before {
		t.Fatal("external-provider error notification was not forwarded")
	}
	owner, _ := store.ThreadOwner("thread-external")
	if owner != controller.ID {
		t.Fatalf("external-provider error changed account ownership: %s", owner)
	}
}

func TestExternalProviderUsageResponseNeverEntersSynchronousFailover(t *testing.T) {
	multiplexer, store, accounts, output := failoverFixture(t)
	controller := accounts[0]
	if err := os.WriteFile(controller.CodexHome+"/config.toml", []byte("model_provider = \"openrouter\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := store.SetThreadOwner("thread-external", controller.ID); err != nil {
		t.Fatal(err)
	}
	message := protocol.Message{
		ID:     protocol.StringID("external-error"),
		Method: "turn/start",
		Params: json.RawMessage(`{"threadId":"thread-external","modelProvider":"openrouter","model":"anthropic/claude"}`),
	}
	key := protocol.RequestIDKey(message.ID)
	multiplexer.externalRoutes[key] = externalRoute{
		accountID: controller.ID, method: message.Method, message: message,
	}
	response := protocol.Message{
		ID: message.ID,
		Error: &protocol.RPCError{
			Code:    -32000,
			Message: "external provider usage limit",
			Data:    json.RawMessage(`{"codexErrorInfo":"usage_limit_exceeded"}`),
		},
	}
	raw, err := protocol.Encode(response)
	if err != nil {
		t.Fatal(err)
	}
	multiplexer.handleInbound(backend.Inbound{AccountID: controller.ID, Message: response, Raw: raw})
	if !bytes.Contains(output.Bytes(), []byte("external provider usage limit")) {
		t.Fatalf("external-provider usage response was not returned unchanged: %s", output.String())
	}
	for _, account := range accounts[1:] {
		child := multiplexer.children[account.ID].(*fakeChild)
		if len(child.sends) != 0 {
			t.Fatalf("external-provider response incorrectly routed to %s: %#v", account.ID, child.sends)
		}
	}
	owner, _ := store.ThreadOwner("thread-external")
	if owner != controller.ID {
		t.Fatalf("external-provider response changed account ownership: %s", owner)
	}
}

func countMethod(methods []string, target string) int {
	count := 0
	for _, method := range methods {
		if method == target {
			count++
		}
	}
	return count
}
