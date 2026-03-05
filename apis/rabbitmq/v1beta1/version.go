/*
Copyright 2025.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1beta1

import (
	"fmt"
	"strconv"
	"strings"
)

// Version represents a parsed RabbitMQ version
type Version struct {
	Major int
	Minor int
	Patch int
}

// ParseRabbitMQVersion parses a version string like "3.9", "3.13.1", "4.2" into components
func ParseRabbitMQVersion(versionStr string) (Version, error) {
	var v Version
	parts := strings.Split(versionStr, ".")

	if len(parts) < 2 {
		return v, fmt.Errorf("invalid version format: %s", versionStr)
	}

	var err error
	v.Major, err = strconv.Atoi(parts[0])
	if err != nil {
		return v, fmt.Errorf("invalid major version: %s", parts[0])
	}

	v.Minor, err = strconv.Atoi(parts[1])
	if err != nil {
		return v, fmt.Errorf("invalid minor version: %s", parts[1])
	}

	if len(parts) > 2 {
		v.Patch, err = strconv.Atoi(parts[2])
		if err != nil {
			return v, fmt.Errorf("invalid patch version: %s", parts[2])
		}
	}

	return v, nil
}

// Is3xTo4xUpgrade returns true if currentVersion is 3.x and targetVersion is 4.x+
func Is3xTo4xUpgrade(currentVersion, targetVersion string) bool {
	current, err := ParseRabbitMQVersion(currentVersion)
	if err != nil {
		return false
	}
	target, err := ParseRabbitMQVersion(targetVersion)
	if err != nil {
		return false
	}
	return current.Major == 3 && target.Major >= 4
}

// IsVersion4OrLater returns true if the version string represents RabbitMQ 4.x or later
func IsVersion4OrLater(versionStr string) bool {
	v, err := ParseRabbitMQVersion(versionStr)
	if err != nil {
		return false
	}
	return v.Major >= 4
}
