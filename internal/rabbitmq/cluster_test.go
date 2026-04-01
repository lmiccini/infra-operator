/*
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

package rabbitmq

import "testing"

func TestTLSVersionsForRabbitMQ(t *testing.T) {
	tests := []struct {
		name        string
		version     string
		fipsEnabled bool
		want        string
	}{
		{"3.x non-FIPS uses TLS 1.2 only", "3.9", false, "['tlsv1.2']"},
		{"3.x FIPS uses TLS 1.2+1.3", "3.9", true, "['tlsv1.2','tlsv1.3']"},
		{"3.13 non-FIPS uses TLS 1.2 only", "3.13", false, "['tlsv1.2']"},
		{"4.x non-FIPS uses TLS 1.2+1.3", "4.2", false, "['tlsv1.2','tlsv1.3']"},
		{"4.x FIPS uses TLS 1.2+1.3", "4.2", true, "['tlsv1.2','tlsv1.3']"},
		{"invalid version non-FIPS uses TLS 1.2 only", "bad", false, "['tlsv1.2']"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := TLSVersionsForRabbitMQ(tt.version, tt.fipsEnabled)
			if got != tt.want {
				t.Errorf("TLSVersionsForRabbitMQ(%q, %v) = %q, want %q", tt.version, tt.fipsEnabled, got, tt.want)
			}
		})
	}
}

func TestRequiresStorageWipe(t *testing.T) {
	tests := []struct {
		name    string
		from    string
		to      string
		want    bool
		wantErr bool
	}{
		{"same version", "3.9", "3.9", false, false},
		{"patch change only", "3.9.0", "3.9.1", false, false},
		{"minor upgrade", "3.9", "3.13", true, false},
		{"major upgrade 3.x to 4.x", "3.9", "4.2", true, false},
		{"major upgrade with patches", "3.13.1", "4.2.1", true, false},
		{"downgrade major", "4.2", "3.9", true, false},
		{"same major different minor", "4.0", "4.2", true, false},
		{"invalid from version", "bad", "4.2", false, true},
		{"invalid to version", "3.9", "bad", false, true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := RequiresStorageWipe(tt.from, tt.to)
			if tt.wantErr {
				if err == nil {
					t.Errorf("expected error for RequiresStorageWipe(%q, %q), got nil", tt.from, tt.to)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tt.want {
				t.Errorf("RequiresStorageWipe(%q, %q) = %v, want %v", tt.from, tt.to, got, tt.want)
			}
		})
	}
}

func TestValidVersionPattern(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  bool
	}{
		{"two-part", "3.9", true},
		{"three-part", "3.13.1", true},
		{"4.x", "4.2", true},
		{"single number", "3", false},
		{"empty", "", false},
		{"alpha", "abc.def", false},
		{"trailing dot", "3.9.", false},
		{"four parts", "3.9.1.2", false},
		{"with prefix", "v3.9", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ValidVersionPattern(tt.input)
			if got != tt.want {
				t.Errorf("ValidVersionPattern(%q) = %v, want %v", tt.input, got, tt.want)
			}
		})
	}
}
