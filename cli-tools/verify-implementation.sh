#!/usr/bin/env bash
# Comprehensive verification script for Deploy Command Consolidation
# Tests all implemented changes without running actual deployments

set -euo pipefail

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Test counters
PASSED=0
FAILED=0
TOTAL=0

# Helper function to run test
run_test() {
    local test_name="$1"
    local test_cmd="$2"
    local expected_pattern="$3"

    TOTAL=$((TOTAL + 1))
    echo -e "\n${YELLOW}Test $TOTAL: $test_name${NC}"

    # If no expected pattern, just check exit code
    if [ -z "$expected_pattern" ]; then
        if eval "$test_cmd" >/dev/null 2>&1; then
            echo -e "${GREEN}✅ PASS${NC}"
            PASSED=$((PASSED + 1))
            return 0
        else
            echo -e "${RED}❌ FAIL${NC}"
            echo "   Command: $test_cmd"
            FAILED=$((FAILED + 1))
            return 1
        fi
    fi

    # If expected pattern provided, check for it in output
    if eval "$test_cmd" 2>&1 | grep -q "$expected_pattern"; then
        echo -e "${GREEN}✅ PASS${NC}"
        PASSED=$((PASSED + 1))
        return 0
    else
        echo -e "${RED}❌ FAIL${NC}"
        echo "   Expected pattern: $expected_pattern"
        echo "   Command: $test_cmd"
        FAILED=$((FAILED + 1))
        return 1
    fi
}

echo "================================"
echo "Deploy Command Consolidation"
echo "Verification Tests"
echo "================================"

# ===== Phase 1: Deploy Command Tests =====
echo -e "\n${YELLOW}===== Phase 1: Deploy Command Tests =====${NC}"

run_test "Deploy help shows targets" \
    "deploy --help" \
    "Targets:"

run_test "Deploy help shows 'local' target" \
    "deploy --help" \
    "local.*Local continuous server"

run_test "Deploy help shows 'debug' target" \
    "deploy --help" \
    "debug.*debugpy"

run_test "Deploy help shows deprecated syntax" \
    "deploy --help" \
    "Legacy Syntax.*deprecated"

run_test "Deploy help shows blocking option" \
    "deploy --help" \
    "blocking"

# ===== Phase 2: Test Command Tests =====
echo -e "\n${YELLOW}===== Phase 2: Test Command Tests =====${NC}"

run_test "Test command exists and is executable" \
    "ls -la ~/cli-tools/bin/test" \
    "test"

run_test "Test help shows input types" \
    "~/cli-tools/bin/test --help" \
    "Input Types"

run_test "Test help shows health check" \
    "~/cli-tools/bin/test --help" \
    "Health check"

run_test "Test help shows Google Chat message" \
    "~/cli-tools/bin/test --help" \
    "Google Chat"

run_test "Test help shows HTTP endpoint" \
    "~/cli-tools/bin/test --help" \
    "endpoint"

run_test "Test help shows --dry-run option" \
    "~/cli-tools/bin/test --help" \
    "dry-run"

run_test "Test help shows --one-shot option" \
    "~/cli-tools/bin/test --help" \
    "one-shot"

run_test "Test help shows examples" \
    "~/cli-tools/bin/test --help" \
    "Examples"

# ===== Phase 3: google-chat-message Deprecation =====
echo -e "\n${YELLOW}===== Phase 3: google-chat-message Deprecation =====${NC}"

run_test "google-chat-message shows deprecation warning in help" \
    "google-chat-message --help" \
    "DEPRECATED"

run_test "google-chat-message help suggests test command" \
    "google-chat-message --help" \
    "Use.*test.*command instead"

# ===== Phase 4: Documentation Tests =====
echo -e "\n${YELLOW}===== Phase 4: Documentation Tests =====${NC}"

run_test "Global CLAUDE.md mentions test command" \
    "grep -q 'test.*Smart testing utility' ~/CLAUDE.md" \
    ""

run_test "Global CLAUDE.md shows deploy targets" \
    "grep -q 'local.*Local continuous server' ~/CLAUDE.md" \
    ""

run_test "Project CLAUDE.md updated with new syntax" \
    "grep -q 'deploy local' ~/ProcAgentDir/ProcurementAgentAI/CLAUDE.md" \
    ""

run_test "README.md includes deploy command" \
    "grep -q 'Deploy.*deploy' ~/cli-tools/README.md" \
    ""

run_test "README.md includes test command" \
    "grep -q 'Test.*test' ~/cli-tools/README.md" \
    ""

# ===== Phase 5: File Existence Tests =====
echo -e "\n${YELLOW}===== Phase 5: File Existence Tests =====${NC}"

run_test "test-command.js exists" \
    "test -f ~/cli-tools/src/deploy/test-command.js" \
    ""

run_test "test wrapper exists and is executable" \
    "test -x ~/cli-tools/bin/test" \
    ""

run_test "deploy wrapper is executable" \
    "test -x ~/cli-tools/bin/deploy" \
    ""

run_test "google-chat-message wrapper is executable" \
    "test -x ~/cli-tools/bin/google-chat-message" \
    ""

# ===== Summary =====
echo -e "\n================================"
echo -e "Test Results Summary"
echo -e "================================"
echo -e "Total tests:  $TOTAL"
echo -e "${GREEN}Passed:       $PASSED${NC}"
if [ $FAILED -gt 0 ]; then
    echo -e "${RED}Failed:       $FAILED${NC}"
else
    echo -e "Failed:       $FAILED"
fi
echo -e "================================"

if [ $FAILED -eq 0 ]; then
    echo -e "\n${GREEN}✅ All tests passed!${NC}"
    exit 0
else
    echo -e "\n${RED}❌ Some tests failed${NC}"
    exit 1
fi
