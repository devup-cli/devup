class DevupCli < Formula
  include Language::Python::Virtualenv

  desc "Polyglot project script discovery and multi-service parallel runner"
  homepage "https://devup-cli.github.io/devup/"
  url "https://files.pythonhosted.org/packages/c4/22/3052906f5f3b4357834475e450f3dcbeca41e7bfa14ea3d5f7c48629ae96/devup_cli-0.3.0.tar.gz"
  sha256 "b122b139d250af786c94f23f5abafcf93d4cccb9681322fd75f65811793ad3c9"
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
