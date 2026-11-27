class DevupCli < Formula
  include Language::Python::Virtualenv

  desc "Polyglot project script discovery and multi-service parallel runner"
  homepage "https://devup-cli.github.io/devup/"
  url "https://files.pythonhosted.org/packages/source/d/devup-cli/devup_cli-0.3.0.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_SHA256"
  license "MIT"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "devup", shell_output("#{bin}/devup --version")
    (testpath / "package.json").write('{"scripts": {"hello": "echo hi"}}')
    output = shell_output("#{bin}/devup -C #{testpath} list --json")
    assert_match "hello", output
  end
end
