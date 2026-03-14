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

import "testing"

func TestParseRabbitMQVersion(t *testing.T) {
	tests := []struct {
		name      string
		input     string
		wantMajor int
		wantMinor int
		wantPatch int
		wantErr   bool
	}{
		{"two-part version", "3.9", 3, 9, 0, false},
		{"three-part version", "3.13.1", 3, 13, 1, false},
		{"4.x version", "4.2", 4, 2, 0, false},
		{"4.x with patch", "4.2.1", 4, 2, 1, false},
		{"single number", "3", 0, 0, 0, true},
		{"empty string", "", 0, 0, 0, true},
		{"non-numeric major", "abc.1", 0, 0, 0, true},
		{"non-numeric minor", "3.abc", 0, 0, 0, true},
		{"non-numeric patch", "3.9.abc", 0, 0, 0, true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			v, err := ParseRabbitMQVersion(tt.input)
			if tt.wantErr {
				if err == nil {
					t.Errorf("expected error for input %q, got nil", tt.input)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error for input %q: %v", tt.input, err)
			}
			if v.Major != tt.wantMajor || v.Minor != tt.wantMinor || v.Patch != tt.wantPatch {
				t.Errorf("ParseRabbitMQVersion(%q) = {%d, %d, %d}, want {%d, %d, %d}",
					tt.input, v.Major, v.Minor, v.Patch, tt.wantMajor, tt.wantMinor, tt.wantPatch)
			}
		})
	}
}

func TestIs3xTo4xUpgrade(t *testing.T) {
	tests := []struct {
		name    string
		current string
		target  string
		want    bool
	}{
		{"3.9 to 4.2", "3.9", "4.2", true},
		{"3.13 to 4.2", "3.13", "4.2", true},
		{"3.13.1 to 4.2.1", "3.13.1", "4.2.1", true},
		{"3.9 to 5.0", "3.9", "5.0", true},
		{"4.0 to 4.2", "4.0", "4.2", false},
		{"4.2 to 3.9", "4.2", "3.9", false},
		{"3.9 to 3.13", "3.9", "3.13", false},
		{"invalid current", "bad", "4.2", false},
		{"invalid target", "3.9", "bad", false},
		{"both invalid", "bad", "bad", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Is3xTo4xUpgrade(tt.current, tt.target)
			if got != tt.want {
				t.Errorf("Is3xTo4xUpgrade(%q, %q) = %v, want %v", tt.current, tt.target, got, tt.want)
			}
		})
	}
}

func TestIsVersion4OrLater(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  bool
	}{
		{"3.9", "3.9", false},
		{"3.13.1", "3.13.1", false},
		{"4.0", "4.0", true},
		{"4.2", "4.2", true},
		{"4.2.1", "4.2.1", true},
		{"5.0", "5.0", true},
		{"invalid", "bad", false},
		{"empty", "", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := IsVersion4OrLater(tt.input)
			if got != tt.want {
				t.Errorf("IsVersion4OrLater(%q) = %v, want %v", tt.input, got, tt.want)
			}
		})
	}
}
