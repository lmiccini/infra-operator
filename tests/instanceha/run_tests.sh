#!/bin/bash

# InstanceHA Test Runner
# This script runs both unit tests and functional tests for the InstanceHA module

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}     InstanceHA Test Suite Runner      ${NC}"
echo -e "${BLUE}========================================${NC}"
echo

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Set PYTHONPATH to include the current directory
export PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH}"

# Function to run tests and capture results
run_test_file() {
    local test_file="$1"
    local test_name="$2"
    
    echo -e "${YELLOW}Running ${test_name}...${NC}"
    echo "----------------------------------------"
    
    if python3 "${SCRIPT_DIR}/${test_file}" 2>&1; then
        echo -e "${GREEN}✅ ${test_name} completed successfully${NC}"
        return 0
    else
        echo -e "${RED}❌ ${test_name} failed${NC}"
        return 1
    fi
}

# Initialize test results
unit_tests_passed=0
functional_tests_passed=0

# Run unit tests
echo -e "${BLUE}Running Unit Tests...${NC}"
if run_test_file "test_instanceha.py" "Unit Tests"; then
    unit_tests_passed=1
fi

echo
echo

# Run functional tests  
echo -e "${BLUE}Running Functional Tests...${NC}"
if run_test_file "functional_test.py" "Functional Tests"; then
    functional_tests_passed=1
fi

echo
echo

# Summary
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}            Test Summary                ${NC}"
echo -e "${BLUE}========================================${NC}"

total_passed=$((unit_tests_passed + functional_tests_passed))
total_tests=2

if [ $unit_tests_passed -eq 1 ]; then
    echo -e "${GREEN}✅ Unit Tests: PASSED${NC}"
else
    echo -e "${RED}❌ Unit Tests: FAILED${NC}"
fi

if [ $functional_tests_passed -eq 1 ]; then
    echo -e "${GREEN}✅ Functional Tests: PASSED${NC}"
else
    echo -e "${RED}❌ Functional Tests: FAILED${NC}"
fi

echo
echo -e "${BLUE}Total: ${total_passed}/${total_tests} test suites passed${NC}"

if [ $total_passed -eq $total_tests ]; then
    echo -e "${GREEN}🎉 All tests completed successfully!${NC}"
    exit 0
else
    echo -e "${RED}⚠️  Some tests failed. Please review the output above.${NC}"
    exit 1
fi
