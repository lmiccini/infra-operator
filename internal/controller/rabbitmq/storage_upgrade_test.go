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

import (
	"testing"

	. "github.com/onsi/ginkgo/v2" //revive:disable:dot-imports
	. "github.com/onsi/gomega"    //revive:disable:dot-imports
	"github.com/openstack-k8s-operators/infra-operator/internal/rabbitmq"
)

func TestStorageUpgrade(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Storage Upgrade Suite")
}

var _ = Describe("ParseRabbitMQVersion", func() {
	It("should parse major.minor version correctly", func() {
		v, err := rabbitmq.ParseRabbitMQVersion("3.13")
		Expect(err).ToNot(HaveOccurred())
		Expect(v.Major).To(Equal(3))
		Expect(v.Minor).To(Equal(13))
		Expect(v.Patch).To(Equal(0))
	})

	It("should parse major.minor.patch version correctly", func() {
		v, err := rabbitmq.ParseRabbitMQVersion("4.2.1")
		Expect(err).ToNot(HaveOccurred())
		Expect(v.Major).To(Equal(4))
		Expect(v.Minor).To(Equal(2))
		Expect(v.Patch).To(Equal(1))
	})

	It("should reject invalid version format", func() {
		_, err := rabbitmq.ParseRabbitMQVersion("4")
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(ContainSubstring("invalid version format"))
	})

	It("should reject non-numeric major version", func() {
		_, err := rabbitmq.ParseRabbitMQVersion("v4.2")
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(ContainSubstring("invalid major version"))
	})

	It("should reject non-numeric minor version", func() {
		_, err := rabbitmq.ParseRabbitMQVersion("4.x")
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(ContainSubstring("invalid minor version"))
	})

	It("should reject non-numeric patch version", func() {
		_, err := rabbitmq.ParseRabbitMQVersion("4.2.beta")
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(ContainSubstring("invalid patch version"))
	})
})

var _ = Describe("requiresStorageWipe", func() {
	It("should not require wipe for same version", func() {
		needsWipe, err := requiresStorageWipe("3.13", "3.13")
		Expect(err).ToNot(HaveOccurred())
		Expect(needsWipe).To(BeFalse())
	})

	It("should not require wipe for patch version changes", func() {
		needsWipe, err := requiresStorageWipe("3.13.0", "3.13.1")
		Expect(err).ToNot(HaveOccurred())
		Expect(needsWipe).To(BeFalse())
	})

	It("should require wipe for major version upgrade", func() {
		needsWipe, err := requiresStorageWipe("3.13", "4.2")
		Expect(err).ToNot(HaveOccurred())
		Expect(needsWipe).To(BeTrue())
	})

	It("should require wipe for major version downgrade", func() {
		needsWipe, err := requiresStorageWipe("4.2", "3.13")
		Expect(err).ToNot(HaveOccurred())
		Expect(needsWipe).To(BeTrue())
	})

	It("should require wipe for minor version upgrade", func() {
		needsWipe, err := requiresStorageWipe("3.9", "3.13")
		Expect(err).ToNot(HaveOccurred())
		Expect(needsWipe).To(BeTrue())
	})

	It("should require wipe for minor version downgrade", func() {
		needsWipe, err := requiresStorageWipe("3.13", "3.9")
		Expect(err).ToNot(HaveOccurred())
		Expect(needsWipe).To(BeTrue())
	})

	It("should handle version with and without patch", func() {
		needsWipe, err := requiresStorageWipe("3.9", "3.9.0")
		Expect(err).ToNot(HaveOccurred())
		Expect(needsWipe).To(BeFalse())
	})

	It("should return error for invalid from version", func() {
		_, err := requiresStorageWipe("invalid", "4.2")
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(ContainSubstring("failed to parse current version"))
	})

	It("should return error for invalid to version", func() {
		_, err := requiresStorageWipe("3.9", "invalid")
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(ContainSubstring("failed to parse target version"))
	})
})

var _ = Describe("Upgrade Constants", func() {
	It("should use a 2-second requeue interval for upgrade checks", func() {
		Expect(UpgradeCheckInterval.Seconds()).To(Equal(float64(2)))
	})
})
