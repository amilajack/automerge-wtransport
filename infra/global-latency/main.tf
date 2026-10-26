variable "regions_to_enable" {
  type    = list(string)
  default = []
}
variable "name" { type = string }
variable "experiment_token" {
  type      = string
  sensitive = true
}
variable "certificate_hash" { type = string }
variable "client_cidr" { type = string }
variable "image_tag" { type = string }
variable "enable_functions" {
  type    = bool
  default = false
}

data "aws_caller_identity" "current" {}
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}
resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.name}-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}
resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_object" "relay" {
  bucket                 = aws_s3_bucket.artifacts.id
  key                    = "relay.tgz"
  source                 = "${path.module}/artifacts/relay.tgz"
  etag                   = filemd5("${path.module}/artifacts/relay.tgz")
  server_side_encryption = "AES256"
}
resource "aws_iam_role" "relay" {
  name               = "${var.name}-relay"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "relay_ssm" {
  role       = aws_iam_role.relay.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "relay_artifact" {
  role   = aws_iam_role.relay.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "s3:GetObject", Resource = aws_s3_object.relay.arn }, { Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.artifacts.arn}/snapshots/*" }] })
}
resource "aws_iam_instance_profile" "relay" {
  name = "${var.name}-relay"
  role = aws_iam_role.relay.name
}
resource "aws_iam_role" "lambda" {
  name               = "${var.name}-lambda"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_security_group" "relay" {
  name   = "${var.name}-relay"
  vpc_id = data.aws_vpc.default.id
  # Lambda public egress IPs are not stable. This disposable relay accepts UDP
  # and TCP globally, protected by a random experiment token and pinned TLS.
  ingress {
    protocol    = "udp"
    from_port   = 4433
    to_port     = 4433
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    protocol    = "tcp"
    from_port   = 4433
    to_port     = 4433
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_instance" "relay" {
  ami                         = data.aws_ssm_parameter.ubuntu.value
  instance_type               = "t4g.small"
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.relay.id]
  iam_instance_profile        = aws_iam_instance_profile.relay.name
  user_data_replace_on_change = true
  metadata_options { http_tokens = "required" }
  root_block_device {
    volume_size           = 12
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }
  user_data  = templatefile("${path.module}/relay.sh.tftpl", { bucket = aws_s3_bucket.artifacts.id })
  tags       = { Name = "${var.name}-relay" }
  depends_on = [aws_iam_role_policy_attachment.relay_ssm, aws_iam_role_policy.relay_artifact, aws_s3_object.relay]
}

output "artifact_bucket" { value = aws_s3_bucket.artifacts.id }
