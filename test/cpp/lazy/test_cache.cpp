#include <gtest/gtest.h>

#include <c10/util/Exception.h>
#include <torch/csrc/lazy/core/cache.h>
#include <torch/csrc/lazy/core/hash.h>
#include <torch/csrc/lazy/core/ir.h>
#include <torch/csrc/lazy/core/shape.h>
#include <torch/csrc/lazy/ts_backend/ts_backend_impl.h>
#include <torch/csrc/lazy/ts_backend/ts_node.h>

namespace torch {
namespace lazy {

// Lazy Tensor is disabled in FBCODE until addressing non-virtual methods (e.g. sizes) in TensorImpl
#ifndef FBCODE_CAFFE2
namespace {
  // This registers the torchscript backend, without which lazy device won't work
static bool inline init_backend(){
  torch::lazy::InitTorchScriptBackend();
  return true;
}
static const bool backend_initialized = init_backend();

}
#endif

class CacheNode : public Node {
 public:
  explicit CacheNode(const std::string& str)
      : Node(OpKind(), /* num_outputs */ 1, /* hash_func */ [&](bool /*bakeInSizes*/) -> hash_t { return Hash(str); }),
        str_(str) {}
  ~CacheNode() override = default;

  const std::vector<Output>& operands() const override {
    TORCH_INTERNAL_ASSERT(false, "Can't access operands of test node");
  }

  const Output& operand(size_t i) const override {
    TORCH_INTERNAL_ASSERT(false, "Can't access operand[i] of test node");
  }
 private:
  std::string str_;
};

TEST(CacheTest, BasicTest) {
  std::shared_ptr<CacheNode> a = std::make_shared<CacheNode>("a");
  std::shared_ptr<CacheNode> b = std::make_shared<CacheNode>("b");
  std::shared_ptr<CacheNode> c = std::make_shared<CacheNode>("c");
  Cache<hash_t, CacheNode, HashReducer> cache(2);

  cache.Add(a->node_hash(), a);
  EXPECT_EQ(cache.Get(a->node_hash()), a);
  EXPECT_EQ(cache.Get(b->node_hash()), nullptr);
  EXPECT_EQ(cache.Get(c->node_hash()), nullptr);

  cache.Add(b->node_hash(), b);
  EXPECT_EQ(cache.Get(a->node_hash()), a);
  EXPECT_EQ(cache.Get(b->node_hash()), b);
  EXPECT_EQ(cache.Get(c->node_hash()), nullptr);

  cache.Add(c->node_hash(), c);
  EXPECT_EQ(cache.Get(a->node_hash()), nullptr); // a has been evicted
  EXPECT_EQ(cache.Get(b->node_hash()), b);
  EXPECT_EQ(cache.Get(c->node_hash()), c);

  cache.Erase(c->node_hash());
  EXPECT_EQ(cache.Get(a->node_hash()), nullptr);
  EXPECT_EQ(cache.Get(b->node_hash()), b);
  EXPECT_EQ(cache.Get(c->node_hash()), nullptr); // c has been removed

  cache.Clear();
  EXPECT_EQ(cache.Get(a->node_hash()), nullptr);
  EXPECT_EQ(cache.Get(b->node_hash()), nullptr);
  EXPECT_EQ(cache.Get(c->node_hash()), nullptr);
}

class CacheNodeWithShape : public TsNode {
 public:
  explicit CacheNodeWithShape(const Shape& shape)
      : TsNode(OpKind(), shape, /* num_outputs */ 1, /* seed */ 0){}
};

#ifndef FBCODE_CAFFE2
TEST(CacheTest, ShapeCacheTestForDynamicShape) {
  // enable dynamic shape
  FLAGS_ltc_enable_dynamic_shapes = true;

  CacheNodeWithShape nodes[] = {
    CacheNodeWithShape(Shape(c10::kFloat, {2, 4})),
    CacheNodeWithShape(Shape(c10::kFloat, {4, 2})) };

  /*
   * Make sure the cached shape for node (2, 4) is not used for node (4, 2)
   */
  for (auto& node : nodes) {
    EXPECT_EQ(node.shape(), getBackend()->GenerateShape([&]() {
      return node.shape();
    }));
  }

  // reset the flag
  FLAGS_ltc_enable_dynamic_shapes = false;
}
#endif

} // namespace lazy
} // namespace torch
